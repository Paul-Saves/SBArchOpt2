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
import logging
import numpy as np
from pymoo.core.algorithm import Algorithm
from pymoo.core.evaluator import Evaluator
from pymoo.core.duplicate import DefaultDuplicateElimination
from pymoo.algorithms.moo.nsga2 import NSGA2, RankAndCrowdingSurvival, MultiObjectiveOutput

from sb_arch_opt.util import capture_log
from sb_arch_opt.problem import ArchOptRepair
from sb_arch_opt.sampling import get_init_sampler, RepairedLatinHypercubeSampling
from sb_arch_opt.algo.pymoo_interface.md_mating import *
from sb_arch_opt.algo.pymoo_interface.storage_restart import *

__all__ = ['provision_pymoo', 'ArchOptNSGA2', 'get_nsga2', 'initialize_from_previous_results', 'ResultsStorageCallback',
           'ExtremeBarrierEvaluator']

log = logging.getLogger('sb_arch_opt.pymoo')


def provision_pymoo(algorithm: Algorithm, init_use_lhs=True, set_init=True, results_folder=None,
                    enable_extreme_barrier=True):
    """
    Provisions a pymoo Algorithm to work correctly for architecture optimization:
    - Sets initializer using a repaired sampler (if `set_init = True`)
    - Sets a repair operator
    - Optionally stores intermediate and final results in some results folder
    - Optionally enables extreme-barrier for dealing with hidden constraints (replace NaN with Inf)
    """
    capture_log()

    if set_init and hasattr(algorithm, 'initialization'):
        algorithm.initialization = get_init_sampler(lhs=init_use_lhs)

    if hasattr(algorithm, 'repair'):
        algorithm.repair = ArchOptRepair()

    if results_folder is not None:
        algorithm.callback = ResultsStorageCallback(results_folder, callback=algorithm.callback)

    if enable_extreme_barrier:
        algorithm.evaluator = ExtremeBarrierEvaluator()

    return algorithm


class ArchOptNSGA2(NSGA2):
    """NSGA2 preconfigured with mixed-variable operators and other architecture optimization measures"""

    def __init__(self,
                 pop_size=100,
                 sampling=RepairedLatinHypercubeSampling(),
                 repair=ArchOptRepair(),
                 mating=MixedDiscreteMating(repair=ArchOptRepair(), eliminate_duplicates=DefaultDuplicateElimination()),
                 eliminate_duplicates=DefaultDuplicateElimination(),
                 survival=RankAndCrowdingSurvival(),
                 output=MultiObjectiveOutput(),
                 results_folder=None,
                 **kwargs):

        evaluator = ExtremeBarrierEvaluator()
        callback = ResultsStorageCallback(results_folder) if results_folder is not None else None

        super().__init__(pop_size=pop_size, sampling=sampling, repair=repair, mating=mating,
                         eliminate_duplicates=eliminate_duplicates, survival=survival, output=output,
                         evaluator=evaluator, callback=callback, **kwargs)


def get_nsga2(pop_size: int, results_folder=None) -> NSGA2:
    """Returns a NSGA2 algorithm preconfigured to work with mixed-discrete variables and other architecture optimization
    measures"""
    capture_log()
    return ArchOptNSGA2(pop_size=pop_size, results_folder=results_folder)


class ExtremeBarrierEvaluator(Evaluator):
    """Evaluator that applies the extreme barrier approach for dealing with hidden constraints: replace NaN with Inf"""

    def _eval(self, problem, pop, evaluate_values_of, **kwargs):
        super()._eval(problem, pop, evaluate_values_of, **kwargs)

        for key in ['F', 'G', 'H']:
            values = pop.get(key)
            values[np.isnan(values)] = np.inf
            pop.set(key, values)

        return pop