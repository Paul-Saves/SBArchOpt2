"""
MIT License

Copyright: (c) 2023, Onera
Contact: remi.lafage@onera.fr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import logging
import numpy as np
from typing import Tuple
from sb_arch_opt.sampling import *
from sb_arch_opt.util import capture_log
from sb_arch_opt.problem import ArchOptProblemBase
from sb_arch_opt.sampling import HierarchicalSampling

import pymoo.core.variable as var
from pymoo.core.population import Population

try:
    import egobox as egx

    HAS_EGOBOX = True
except ImportError:
    HAS_EGOBOX = False

__all__ = ["HAS_EGOBOX", "check_dependencies", "EGORArchOptInterface"]

log = logging.getLogger("sb_arch_opt.egor")


def check_dependencies():
    if not HAS_EGOBOX:
        raise ImportError(f"egobox not installed!")


class EGORArchOptInterface:
    """
    Class for interfacing with EGOR
    """

    def __init__(
        self,
        problem: ArchOptProblemBase,
        results_folder: str,
        n_init: int,
        seed: "None|int",
    ):
        check_dependencies()
        self._problem = problem
        self._results_folder = results_folder
        self.n_init = n_init
        self.n_infill = None
        self._seed = seed
        self._egor = None

        self._design_space = None
        self._x = None
        self._x_failed = None
        self._y = None

    @property
    def x(self) -> np.ndarray:
        """Design vectors"""
        if self._x is None:
            return np.zeros((0, self._problem.n_var))
        return self._x

    @property
    def n(self) -> int:
        """Number of available successfully evaluated points"""
        return 0 if self._x is None else self._x.shape[0]

    @property
    def x_failed(self) -> np.ndarray:
        """Design vectors"""
        if self._x_failed is None:
            return np.zeros((0, self._problem.n_var))
        return self._x_failed

    @property
    def n_failed(self) -> int:
        """Number of failed points"""
        return 0 if self._x_failed is None else self._x_failed.shape[0]

    @property
    def n_tried(self):
        """Number of points that were tried to be evaluated (n + n_failed)"""
        return self.n + self.n_failed

    @property
    def y(self) -> np.ndarray:
        """All outputs: f, g, h"""
        if self._y is None:
            p = self._problem
            return np.zeros((0, p.n_obj + p.n_ieq_constr + p.n_eq_constr))
        return self._y

    @property
    def f(self) -> np.ndarray:
        """Objective values"""
        f, _, _ = self._split_y(self.y)
        return f

    @property
    def g(self) -> np.ndarray:
        """Inequality constraints"""
        _, g, _ = self._split_y(self.y)
        return g

    @property
    def h(self) -> np.ndarray:
        """Equality constraints"""
        _, _, h = self._split_y(self.y)
        return h

    @property
    def pop(self) -> Population:
        """Population of all evaluated points"""
        return self.get_population(self.x, self.y)

    @property
    def opt(self) -> Population:
        """Optimal points (Pareto front if multi-objective)"""
        return self._get_pareto_front(self.pop)

    def initialize_from_previous(self, results_folder: str = None):
        capture_log()
        if results_folder is None:
            results_folder = self._results_folder

        # Load from problem state
        population = self._problem.load_previous_results(results_folder)
        if population is not None:
            self._x, self._x_failed, self._y = self._get_xy(population)
            log.info(
                f"Previous results loaded from problem results: {len(population)} design points "
                f"({self.n} ok, {self.n_failed} failed)"
            )
            return

        log.info("No previous results found")

    def minimize(self, n_infill: int):
        capture_log()

        # Automatically initialize from previous results if reusing the same storage folder
        if self._x is None:
            self.initialize_from_previous()

        # Run DOE if needed
        n_available = self.n_tried
        if n_available < self.n_init:
            log.info(
                f"Running DOE of {self.n_init-n_available} points ({self.n_init} total)"
            )
            self._run_doe(self.n_init - n_available)

        # Run optimization
        n_available = self.n_tried
        if n_available < self.n_init + n_infill:
            n_infills = n_infill - (n_available - self.n_init)
            log.info(
                f"Running optimization: {n_infills} infill points (ok DOE points: {self.n})"
            )
            opt_res = self._run_infills(n_infills)

        # Save final results and return Pareto front
        self._save_results()

        return opt_res

    def _run_doe(self, n: int = None):
        if n is None:
            n = self.n_init

        x_doe = self._sample_doe(n)
        print(x_doe)
        self._x, self._x_failed, self._y = self._get_xy(self._evaluate(x_doe))

        if self._x.shape[0] < 2:
            log.info(
                f"Not enough points sampled ({self._x.shape[0]} success, {self._x_failed.shape[0]} failed),"
                f"problems with model fitting can be expected"
            )

        self._save_results()

    def _sample_doe(self, n: int) -> np.ndarray:
        return HierarchicalSampling().do(self._problem, n).get("X")

    def _run_infills(self, n_infills: int):
        for i in range(n_infills):
            # Ask for a new infill point
            log.info(
                f"Getting new infill point {i+1}/{n_infills} (point {self._x.shape[0]+1} overall)"
            )
            x = self.egor.suggest(self._x, self._y)

            # Evaluate and impute
            log.info(
                f"Evaluating point {i+1}/{n_infills} (point {self._x.shape[0]+1} overall)"
            )
            x, x_failed, y = self._get_xy(self._evaluate(np.array(x)))
            # Update and save DOE
            self._x = np.row_stack([self._x, x])
            self._y = np.row_stack([self._y, y])
            self._x_failed = np.row_stack([self._x_failed, x_failed])
            self._save_results()

        return self.egor.get_result(self._x, self.y)

    @property
    def egor(self):
        if self._egor is None:
            kpls_dim = None
            if self._problem.n_var > 9:
                kpls_dim = 3

            self._egor = egx.Egor(
                self.design_space,
                n_cstr=self._get_constraints_nb(),
                seed=self._seed,
                kpls_dim=kpls_dim,
            )
        return self._egor

    @property
    def design_space(self) -> "list[egx.XSpec]":
        if self._design_space is None:
            self._design_space = []
            for var_def in self._problem.des_vars:
                if isinstance(var_def, var.Real):
                    self._design_space.append(
                        egx.XSpec(
                            egx.XType.FLOAT, [var_def.bounds[0], var_def.bounds[1]]
                        )
                    )

                elif isinstance(var_def, var.Integer):
                    self._design_space.append(
                        egx.XSpec(egx.XType.INT, [var_def.bounds[0], var_def.bounds[1]])
                    )

                elif isinstance(var_def, var.Binary):
                    self._design_space.append(egx.XSpec(egx.XType.INT, [0, 1]))

                elif isinstance(var_def, var.Choice):
                    print(f"ENUM [{len(var_def.options)}]")
                    self._design_space.append(
                        egx.XSpec(egx.XType.ENUM, [len(var_def.options)])
                    )

                else:
                    raise RuntimeError(f"Unsupported design variable type: {var_def!r}")

        return self._design_space

    def _get_constraints_nb(self):
        constraints_nb = self._problem.n_ieq_constr
        if self._problem.n_eq_constr:
            raise NotImplementedError(
                "Egor does not handle equality constraints, only negative ineq constraints (c <= 0)"
            )
        return constraints_nb

    def _save_results(self):
        self._problem.store_results(self._results_folder)

    def _evaluate(self, x: np.ndarray) -> Population:
        """
        Evaluates a list of design points (x is a matrix of size n x nx). A population is returned with matrices:
        - X: imputed design vectors
        - is_active: activeness vectors (booleans defining which design variable is active in each design vector)
        - F: objective values
        - G: inequality constraints (None if there are no inequality constraints)
        - H: equality constraints (None if there are no equality constraints)
        """
        out = self._problem.evaluate(x, return_as_dictionary=True)
        return Population.new(**out)

    def _get_xy(
        self, population: Population
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Concatenate evaluation outputs (F, G, H) and split x into evaluated and failed points.
        Returns: x, x_failed, y"""

        # Concatenate outputs
        outputs = [population.get("F")]
        if self._problem.n_ieq_constr > 0:
            outputs.append(population.get("G"))
        if self._problem.n_eq_constr > 0:
            outputs.append(population.get("H"))
        y = np.column_stack(outputs)

        # Split x into ok and failed points
        x = population.get("X")
        is_failed = self._problem.get_failed_points(population)
        x_failed = x[is_failed, :]
        x = x[~is_failed, :]
        y = y[~is_failed, :]

        return x, x_failed, y

    def _split_y(self, y: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Split outputs (y) into F, G, H"""
        f, y = np.split(y, [self._problem.n_obj], axis=1)

        if self._problem.n_ieq_constr > 0:
            g, y = np.split(y, [self._problem.n_ieq_constr], axis=1)
        else:
            g = np.zeros((y.shape[0], 0))

        if self._problem.n_eq_constr > 0:
            h = y[:, : self._problem.n_eq_constr]
        else:
            h = np.zeros((y.shape[0], 0))

        return f, g, h

    def get_population(self, x: np.ndarray, y: np.ndarray) -> Population:
        f, g, h = self._split_y(y)
        kwargs = {"X": x, "F": f, "G": g, "H": h}
        pop = Population.new(**kwargs)
        return pop
