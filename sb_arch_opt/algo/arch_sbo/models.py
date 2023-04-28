"""
Licensed under the GNU General Public License, Version 3.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.gnu.org/licenses/gpl-3.0.html.en

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Copyright: (c) 2023, Deutsches Zentrum fuer Luft- und Raumfahrt e.V.
Contact: jasper.bussemaker@dlr.de
"""
import numpy as np
from typing import *
from dataclasses import dataclass
import pymoo.core.variable as var
from pymoo.core.problem import Problem
from sb_arch_opt.problem import ArchOptProblemBase
from sb_arch_opt.design_space import ArchDesignSpace
from sb_arch_opt.sampling import HierarchicalRandomSampling
from pymoo.util.normalization import Normalization, SimpleZeroToOneNormalization

try:
    from smt.surrogate_models.rbf import RBF
    from smt.surrogate_models.surrogate_model import SurrogateModel

    from smt.surrogate_models.krg import KRG
    from smt.surrogate_models.kpls import KPLS
    from smt.surrogate_models.krg_based import MixIntKernelType, MixHrcKernelType
    from smt.applications.mixed_integer import MixedIntegerKrigingModel

    from smt.utils.mixed_integer import XType
    from smt.utils.kriging import XSpecs, XRole
    from smt.utils.design_space import BaseDesignSpace
    import smt.utils.design_space as ds

    # Temp fix: fix class name of XRole enum
    from enum import Enum
    import smt.utils.kriging as krg_utils
    krg_utils.XRole = Enum("XRole", ["NEUTRAL", "META", "DECREED"])

    HAS_ARCH_SBO = True
except ImportError:
    HAS_ARCH_SBO = False

    class BaseDesignSpace:
        pass

__all__ = ['check_dependencies', 'HAS_ARCH_SBO', 'ModelFactory', 'MixedDiscreteNormalization', 'SBArchOptDesignSpace']


def check_dependencies():
    if not HAS_ARCH_SBO:
        raise ImportError(f'arch_sbo dependencies not installed: python setup.py install[arch_sbo]')


@dataclass
class SMTDesignSpaceSpec:
    var_defs: List[dict]  # [{'name': name, 'lb': lb, 'ub', ub}, ...]
    var_types: List[Union[str, Tuple[str, int]]]  # FLOAT, INT, ENUM
    var_limits: List[Union[Tuple[float, float], list]]  # Bounds (options for an enum)
    design_space: 'SBArchOptDesignSpace'
    is_mixed_discrete: bool


class MixedDiscreteNormalization(Normalization):
    """Normalizes continuous variables to [0, 1], moves integer variables to start at 0"""

    def __init__(self, design_space: ArchDesignSpace):
        self._design_space = design_space
        self._is_cont_mask = design_space.is_cont_mask
        self._is_int_mask = design_space.is_int_mask
        super().__init__()

    def forward(self, x):
        x_norm = x.copy()
        xl, xu = self._design_space.xl, self._design_space.xu

        norm = xu - xl
        norm[norm == 0] = 1e-32

        cont_mask = self._is_cont_mask
        x_norm[:, cont_mask] = (x[:, cont_mask] - xl[cont_mask]) / norm[cont_mask]

        int_mask = self._is_int_mask
        x_norm[:, int_mask] = x[:, int_mask] - xl[int_mask]

        return x_norm

    def backward(self, x):
        x_abs = x.copy()
        xl, xu = self._design_space.xl, self._design_space.xu

        cont_mask = self._is_cont_mask
        x_abs[:, cont_mask] = x[:, cont_mask]*(xu[cont_mask]-xl[cont_mask]) + xl[cont_mask]

        int_mask = self._is_int_mask
        x_abs[:, int_mask] = x[:, int_mask] + xl[int_mask]

        return x_abs


class ModelFactory:

    def __init__(self, problem: ArchOptProblemBase):
        self.problem = problem

    def get_smt_design_space_spec(self) -> SMTDesignSpaceSpec:
        """Get information about the design space as needed by SMT and SEGOMOE"""
        check_dependencies()
        return self.create_smt_design_space_spec(self.problem.design_space)

    @staticmethod
    def create_smt_design_space_spec(arch_design_space: ArchDesignSpace, md_normalize=False):
        check_dependencies()

        design_space = SBArchOptDesignSpace(arch_design_space, md_normalize=md_normalize)
        is_mixed_discrete = not np.all(arch_design_space.is_cont_mask)

        var_types = design_space.get_x_types()
        var_limits = design_space.get_x_limits()
        var_defs = [{'name': f'x{i}', 'lb': bounds[0], 'ub': bounds[1]}
                    for i, bounds in enumerate(design_space.get_num_bounds())]

        return SMTDesignSpaceSpec(
            var_defs=var_defs,
            var_types=var_types,
            var_limits=var_limits,
            design_space=design_space,
            is_mixed_discrete=is_mixed_discrete,
        )

    @staticmethod
    def get_continuous_normalization(problem: Problem):
        return SimpleZeroToOneNormalization(xl=problem.xl, xu=problem.xu, estimate_bounds=False)

    def get_md_normalization(self):
        return MixedDiscreteNormalization(self.problem.design_space)

    @staticmethod
    def get_rbf_model():
        check_dependencies()
        return RBF(print_global=False, d0=1., poly_degree=-1, reg=1e-10)

    @staticmethod
    def get_kriging_model(**kwargs):
        check_dependencies()
        return KRG(print_global=False, **kwargs)

    def get_md_kriging_model(self, kpls_n_comp: int = None, **kwargs) -> Tuple['SurrogateModel', Normalization]:
        check_dependencies()
        normalization = self.get_md_normalization()
        norm_ds_spec = self.create_smt_design_space_spec(self.problem.design_space, md_normalize=True)

        kwargs.update(
            print_global=False,
            design_space=norm_ds_spec.design_space,
            categorical_kernel=MixIntKernelType.EXP_HOMO_HSPHERE,
            hierarchical_kernel=MixHrcKernelType.ALG_KERNEL,
        )
        if norm_ds_spec.is_mixed_discrete:
            kwargs['n_start'] = kwargs.get('n_start', 5)

        if kpls_n_comp is not None:
            surrogate = KPLS(n_comp=kpls_n_comp, **kwargs)
        else:
            surrogate = KRG(**kwargs)

        if norm_ds_spec.is_mixed_discrete:
            surrogate = MixedIntegerKrigingModel(surrogate=surrogate)
        return surrogate, normalization


class SBArchOptDesignSpace(BaseDesignSpace):
    """SMT design space implementation using SBArchOpt's design space logic"""

    def __init__(self, arch_design_space: ArchDesignSpace, md_normalize=False):
        self._ds = arch_design_space
        self.normalize = MixedDiscreteNormalization(arch_design_space) if md_normalize else None
        super().__init__()

    @property
    def arch_design_space(self) -> ArchDesignSpace:
        return self._ds

    def _get_design_variables(self) -> List[ds.DesignVariable]:
        """Return the design variables defined in this design space if not provided upon initialization of the class"""
        smt_des_vars = []
        is_conditional = self._ds.is_conditionally_active
        normalize = self.normalize is not None
        for i, dv in enumerate(self._ds.des_vars):
            if isinstance(dv, var.Real):
                bounds = (0, 1) if normalize else dv.bounds
                smt_des_vars.append(ds.FloatVariable(bounds[0], bounds[1]))

            elif isinstance(dv, var.Integer):
                bounds = (0, dv.bounds[1]-dv.bounds[0]) if normalize else dv.bounds
                smt_des_vars.append(ds.IntegerVariable(bounds[0], bounds[1]))

            elif isinstance(dv, var.Binary):
                smt_des_vars.append(ds.OrdinalVariable(values=[0, 1]))

            elif isinstance(dv, var.Choice):
                # Conditional categorical variables are currently not supported
                if is_conditional[i]:
                    smt_des_vars.append(ds.IntegerVariable(0, len(dv.options)-1))
                else:
                    smt_des_vars.append(ds.CategoricalVariable(values=dv.options))

            else:
                raise ValueError(f'Unexpected variable type: {dv!r}')
        return smt_des_vars

    def _is_conditionally_acting(self) -> np.ndarray:
        return self._ds.is_conditionally_active

    def _correct_get_acting(self, x: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        if self.normalize is not None:
            x = self.normalize.backward(x)

        x, is_active = self._ds.correct_x(x)

        if self.normalize is not None:
            x = self.normalize.forward(x)
        return x, is_active

    def _sample_valid_x(self, n: int) -> Tuple[np.ndarray, np.ndarray]:
        sampler = HierarchicalRandomSampling()
        stub_problem = ArchOptProblemBase(self._ds)
        x, is_active = sampler.sample_get_x(stub_problem, n)

        if self.normalize is not None:
            x = self.normalize.forward(x)
        return x, is_active

    def __str__(self):
        return 'SBArchOpt Design Space'

    def __repr__(self):
        return f'{self.__class__.__name__}({self._ds!r})'