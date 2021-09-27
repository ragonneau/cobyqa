import copy
import warnings

import numpy as np

from .linalg import bvcs, bvlag, bvtcg, cpqp, givens, lctcg, nnls
from .utils import RestartRequiredException, omega_product


class TrustRegion:
    r"""
    Represent the states of a nonlinear constrained problem.
    """

    def __init__(self, fun, x0, args=(), xl=None, xu=None, Aub=None, bub=None,
                 Aeq=None, beq=None, cub=None, ceq=None, options=None,
                 **kwargs):
        r"""
        Initialize the states of the nonlinear constrained problem.
        """
        self._fun = fun
        x0 = np.array(x0, dtype=float)
        n = x0.size
        if not isinstance(args, tuple):
            args = (args,)
        self._args = args
        if xl is None:
            xl = np.full_like(x0, -np.inf)
        xl = np.array(xl, dtype=float)
        if xu is None:
            xu = np.full_like(x0, np.inf)
        xu = np.array(xu, dtype=float)
        if Aub is None:
            Aub = np.empty((0, n))
        Aub = np.array(Aub, dtype=float)
        if bub is None:
            bub = np.empty(0)
        bub = np.array(bub, dtype=float)
        if Aeq is None:
            Aeq = np.empty((0, n))
        Aeq = np.array(Aeq, dtype=float)
        if beq is None:
            beq = np.empty(0)
        beq = np.array(beq, dtype=float)
        if options is None:
            options = {}
        self._cub = cub
        self._ceq = ceq
        self._options = dict(options)
        self.set_default_options(n)
        self.check_options(n)

        # Project the initial guess onto the bound constraints.
        x0 = np.minimum(xu, np.maximum(xl, x0))

        # Modify the initial guess in order to avoid conflicts between the
        # bounds and the first quadratic models. The initial components of the
        # initial guess should either equal bound components or allow the
        # projection of the initial trust region onto the components to lie
        # entirely inside the bounds.
        rhobeg = self.rhobeg
        rhoend = self.rhoend
        rhobeg = min(.5 * np.min(xu - xl), rhobeg)
        rhoend = min(rhobeg, rhoend)
        self._options.update({'rhobeg': rhobeg, 'rhoend': rhoend})
        adj = (x0 - xl <= rhobeg) & (xl < x0)
        if np.any(adj):
            x0[adj] = xl[adj] + rhobeg
        adj = (xu - x0 <= rhobeg) & (x0 < xu)
        if np.any(adj):
            x0[adj] = xu[adj] - rhobeg

        # Set the initial shift of the origin, designed to manage the effects
        # of computer rounding errors in the calculations, and update
        # accordingly the right-hand sides of the constraints at most linear.
        self._xbase = x0

        # Set the initial models of the problem.
        self._models = Models(self.fun, self._xbase, xl, xu, Aub, bub, Aeq, beq,
                              self.cub, self.ceq, self._options)
        if self.debug:
            self.check_models()

        # Determine the initial least-squares multipliers of the problem.
        self._penub = 0.
        self._peneq = 0.
        self._lmlub = np.zeros_like(bub)
        self._lmleq = np.zeros_like(beq)
        self._lmnlub = np.zeros(self.mnlub, dtype=float)
        self._lmnleq = np.zeros(self.mnleq, dtype=float)
        self.update_multipliers(**kwargs)

        # Evaluate the merit function at the interpolation points and
        # determine the optimal point so far and update the initial models.
        # self.kopt = self.get_best_point()
        npt = self.npt
        mval = np.empty(npt, dtype=float)
        for k in range(npt):
            mval[k] = self(self.xpt[k, :], self.fval[k], self.cvalub[k, :],
                           self.cvaleq[k, :])
        self.kopt = np.argmin(mval)
        if self.debug:
            self.check_models()

        # The initial step is a trust-region step.
        self._knew = None

    def __call__(self, x, fx, cubx, ceqx, model=False):
        r"""
        Evaluate the merit functions at ``x``. If ``model = True`` is provided,
        the method also returns the value of the merit function corresponding to
        the modeled problem.
        """
        tiny = np.finfo(float).tiny
        ax = fx
        mx = 0.
        if abs(self._penub) > tiny * np.max(np.abs(self._lmlub), initial=0.):
            tub = np.dot(self.aub, x) - self.bub + self._lmlub / self._penub
            tub = np.maximum(0., tub)
            alub = .5 * self._penub * np.inner(tub, tub)
            ax += alub
            mx += alub
        lmnlub_max = np.max(np.abs(self._lmnlub), initial=0.)
        if abs(self._penub) > tiny * lmnlub_max:
            tub = cubx + self._lmnlub / self._penub
            tub = np.maximum(0., tub)
            ax += .5 * self._penub * np.inner(tub, tub)
        if abs(self._peneq) > tiny * np.max(np.abs(self._lmleq), initial=0.):
            teq = np.dot(self.aeq, x) - self.beq + self._lmleq / self._peneq
            aleq = .5 * self._peneq * np.inner(teq, teq)
            ax += aleq
            mx += aleq
        lmnleq_max = np.max(np.abs(self._lmnleq), initial=0.)
        if abs(self._peneq) > tiny * lmnleq_max:
            teq = ceqx + self._lmnleq / self._peneq
            ax += .5 * self._peneq * np.inner(teq, teq)
        if model:
            mx += self.model_obj(x)
            if abs(self._penub) > tiny * lmnlub_max:
                tub = self._lmnlub / self._penub
                for i in range(self.mnlub):
                    tub[i] += self.model_cub(x, i)
                tub = np.maximum(0., tub)
                mx += .5 * self._penub * np.inner(tub, tub)
            if abs(self._peneq) > tiny * lmnleq_max:
                teq = self._lmnleq / self._peneq
                for i in range(self.mnleq):
                    teq[i] += self.model_ceq(x, i)
                mx += .5 * self._peneq * np.inner(teq, teq)
            return ax, mx
        return ax

    def __getattr__(self, item):
        try:
            return self._options[item]
        except KeyError as e:
            raise AttributeError(item) from e

    @property
    def xbase(self):
        r"""
        Return the shift of the origin in the calculations.
        """
        return self._xbase

    @property
    def options(self):
        r"""
        Return the option passed to the solver.
        """
        return self._options

    @property
    def penub(self):
        r"""
        Returns the penalty coefficient for the inequality constraints.
        """
        return self._penub

    @property
    def peneq(self):
        r"""
        Returns the penalty coefficient for the equality constraints.
        """
        return self._peneq

    @property
    def lmlub(self):
        return self._lmlub

    @property
    def lmleq(self):
        return self._lmleq

    @property
    def lmnlub(self):
        return self._lmnlub

    @property
    def lmnleq(self):
        return self._lmnleq

    @property
    def knew(self):
        return self._knew

    @property
    def xl(self):
        return self._models.xl

    @property
    def xu(self):
        return self._models.xu

    @property
    def aub(self):
        return self._models.aub

    @property
    def bub(self):
        return self._models.bub

    @property
    def mlub(self):
        return self._models.mlub

    @property
    def aeq(self):
        return self._models.aeq

    @property
    def beq(self):
        return self._models.beq

    @property
    def mleq(self):
        return self._models.mleq

    @property
    def xpt(self):
        r"""
        Return the interpolation points.
        """
        return self._models.xpt

    @property
    def fval(self):
        r"""
        Return the values of the objective function at the interpolation points.
        """
        return self._models.fval

    @property
    def rval(self):
        return self._models.rval

    @property
    def cvalub(self):
        return self._models.cvalub

    @property
    def mnlub(self):
        return self._models.mnlub

    @property
    def cvaleq(self):
        return self._models.cvaleq

    @property
    def mnleq(self):
        return self._models.mnleq

    @property
    def kopt(self):
        r"""
        Return the index of the best point so far.
        """
        return self._models.kopt

    @kopt.setter
    def kopt(self, knew):
        r"""
        Set the index of the best point so far.
        """
        self._models.kopt = knew

    @property
    def xopt(self):
        r"""
        Return the best point so far.
        """
        return self._models.xopt

    @property
    def fopt(self):
        r"""
        Return the value of the objective function at the best point so far.
        """
        return self._models.fopt

    @property
    def maxcv(self):
        r"""
        Return the constraint violation at the best point so far.
        """
        return self._models.ropt

    @property
    def coptub(self):
        return self._models.coptub

    @property
    def copteq(self):
        return self._models.copteq

    @property
    def type(self):
        return self._models.type

    @property
    def is_model_step(self):
        return self._knew is not None

    def fun(self, x):
        fx = float(self._fun(x, *self._args))
        if self.disp:
            print(f'{self._fun.__name__}({x}) = {fx}.')
        return fx

    def cub(self, x):
        return self._eval_con(self._cub, x)

    def ceq(self, x):
        return self._eval_con(self._ceq, x)

    def model_obj(self, x):
        return self._models.obj(x)

    def model_obj_grad(self, x):
        return self._models.obj_grad(x)

    def model_obj_hess(self):
        return self._models.obj_hess()

    def model_obj_hessp(self, x):
        return self._models.obj_hessp(x)

    def model_obj_curv(self, x):
        return self._models.obj_curv(x)

    def model_obj_alt(self, x):
        return self._models.obj_alt(x)

    def model_obj_alt_grad(self, x):
        return self._models.obj_alt_grad(x)

    def model_obj_alt_hess(self):
        return self._models.obj_alt_hess()

    def model_obj_alt_hessp(self, x):
        return self._models.obj_alt_hessp(x)

    def model_obj_alt_curv(self, x):
        return self._models.obj_alt_curv(x)

    def model_cub(self, x, i):
        return self._models.cub(x, i)

    def model_cub_grad(self, x, i):
        return self._models.cub_grad(x, i)

    def model_cub_hess(self, i):
        return self._models.cub_hess(i)

    def model_cub_hessp(self, x, i):
        return self._models.cub_hessp(x, i)

    def model_cub_curv(self, x, i):
        return self._models.cub_curv(x, i)

    def model_cub_alt(self, x, i):
        return self._models.cub_alt(x, i)

    def model_cub_alt_grad(self, x, i):
        return self._models.cub_alt_grad(x, i)

    def model_cub_alt_hess(self, i):
        return self._models.cub_alt_hess(i)

    def model_cub_alt_hessp(self, x, i):
        return self._models.cub_alt_hessp(x, i)

    def model_cub_alt_curv(self, x, i):
        return self._models.cub_alt_curv(x, i)

    def model_ceq(self, x, i):
        return self._models.ceq(x, i)

    def model_ceq_grad(self, x, i):
        return self._models.ceq_grad(x, i)

    def model_ceq_hess(self, i):
        return self._models.ceq_hess(i)

    def model_ceq_hessp(self, x, i):
        return self._models.ceq_hessp(x, i)

    def model_ceq_curv(self, x, i):
        return self._models.ceq_curv(x, i)

    def model_ceq_alt(self, x, i):
        return self._models.ceq_alt(x, i)

    def model_ceq_alt_grad(self, x, i):
        return self._models.ceq_alt_grad(x, i)

    def model_ceq_alt_hess(self, i):
        return self._models.ceq_alt_hess(i)

    def model_ceq_alt_hessp(self, x, i):
        return self._models.ceq_alt_hessp(x, i)

    def model_ceq_alt_curv(self, x, i):
        return self._models.ceq_alt_curv(x, i)

    def model_lag(self, x):
        return self._models.lag(x, self._lmlub, self._lmleq, self._lmnlub,
                                self._lmnleq)

    def model_lag_grad(self, x):
        return self._models.lag_grad(x, self._lmlub, self._lmleq, self._lmnlub,
                                     self._lmnleq)

    def model_lag_hess(self):
        return self._models.lag_hess(self._lmnlub, self._lmnleq)

    def model_lag_hessp(self, x):
        r"""
        Evaluate the product of the Hessian matrix of the Lagrangian function of
        the model and ``x``.
        """
        return self._models.lag_hessp(x, self._lmnlub, self._lmnleq)

    def set_default_options(self, n):
        r"""
        Set the default options of the solvers.
        """
        rhoend = getattr(self, 'rhoend', 1e-6)
        self._options.setdefault('rhobeg', max(1., rhoend))
        self._options.setdefault('rhoend', min(rhoend, self.rhobeg))
        self._options.setdefault('npt', 2 * n + 1)
        self._options.setdefault('maxfev', max(500 * n, self.npt + 1))
        self._options.setdefault('target', -np.inf)
        self._options.setdefault('disp', False)
        self._options.setdefault('debug', False)

    def check_options(self, n, stack_level=2):
        r"""
        Set the options passed to the solvers.
        """
        # Ensure that the option 'npt' is in the required interval.
        npt_min = n + 2
        npt_max = (n + 1) * (n + 2) // 2
        npt = self.npt
        if not (npt_min <= npt <= npt_max):
            self._options['npt'] = min(npt_max, max(npt_min, npt))
            message = "Option 'npt' is not in the required interval and is "
            message += 'increased.' if npt_min > npt else 'decreased.'
            warnings.warn(message, RuntimeWarning, stacklevel=stack_level)

        # Ensure that the option 'maxfev' is large enough.
        maxfev = self.maxfev
        if maxfev <= self.npt:
            self._options['maxfev'] = self.npt + 1
            if maxfev <= npt:
                message = "Option 'maxfev' is too low and is increased."
            else:
                message = "Option 'maxfev' is correspondingly increased."
            warnings.warn(message, RuntimeWarning, stacklevel=stack_level)

        # Ensure that the options 'rhobeg' and 'rhoend' are consistent.
        if self.rhoend > self.rhobeg:
            self._options['rhoend'] = self.rhobeg
            message = "Option 'rhoend' is too large and is decreased."
            warnings.warn(message, RuntimeWarning, stacklevel=stack_level)

    def get_best_point(self):
        kopt = self.kopt
        mopt = self(self.xopt, self.fopt, self.coptub, self.copteq)
        for k in range(self.npt):
            if k != kopt:
                mval = self(self.xpt[k, :], self.fval[k], self.cvalub[k, :],
                            self.cvaleq[k, :])
                if self.less_merit(mval, self.rval[k], mopt, self.rval[kopt]):
                    kopt = k
                    mopt = mval
        return kopt

    def prepare_trust_region_step(self):
        self._knew = None

    def prepare_model_step(self, delta):
        r"""
        Get the index of the further point from ``self.xopt`` if the
        corresponding distance is more than ``delta``, -1 otherwise.
        """
        dsq = np.sum((self.xpt - self.xopt[np.newaxis, :]) ** 2., axis=1)
        dsq[dsq <= delta ** 2.] = -np.inf
        if np.any(np.isfinite(dsq)):
            self._knew = np.argmax(dsq)
        else:
            self._knew = None

    def less_merit(self, mval1, rval1, mval2, rval2):
        eps = np.finfo(float).eps
        tol = 10. * eps * self.npt * max(1., abs(mval2))
        if mval1 < mval2:
            return True
        elif max(self._penub, self._peneq) < tol:
            if abs(mval1 - mval2) <= tol and rval1 < rval2:
                return True
        return False

    def shift_origin(self, delta):
        r"""
        Update the shift of the origin if necessary.
        """
        xoptsq = np.inner(self.xopt, self.xopt)

        # Update the shift from the origin only if the displacement from the
        # shift of the best point is substantial in the trust region.
        if xoptsq >= 10. * delta ** 2.:
            # Update the models of the problem to include the new shift.
            self._xbase += self.xopt
            self._models.shift_origin()
            if self.debug:
                self.check_models()

    def update(self, step, **kwargs):
        r"""
        Update the model to include the trial point in the interpolation set.
        """
        # Evaluate the objective function at the trial point.
        xsav = np.copy(self.xopt)
        xnew = xsav + step
        fx = self.fun(self._xbase + xnew)
        cubx = self.cub(self._xbase + xnew)
        ceqx = self.ceq(self._xbase + xnew)

        # Update the Lagrange multipliers and the penalty parameters.
        self.update_multipliers(**kwargs)
        ksav = self.kopt
        mx, mmx, mopt = self.update_penalty_coefficients(xnew, fx, cubx, ceqx)
        if ksav != self.kopt:
            self.prepare_trust_region_step()
            raise RestartRequiredException

        # Determine the trust-region ratio.
        tiny = np.finfo(float).tiny
        if not self.is_model_step and abs(mopt - mmx) > tiny * abs(mopt - mx):
            ratio = (mopt - mx) / (mopt - mmx)
        else:
            ratio = -1.

        # Update the models of the problem. The step is updated to take into
        # account the fact that the best point so far may have been updated when
        # the penalty coefficients have been updated.
        step += xsav - self.xopt
        rx = self._models.resid(xnew, cubx, ceqx)
        self._knew = self._models.update(step, fx, cubx, ceqx, self._knew)
        if self.less_merit(mx, rx, mopt, self.maxcv):
            self.kopt = self._knew
            mopt = mx
        if self.debug:
            self.check_models()
        return mopt, ratio

    def update_multipliers(self, **kwargs):
        r"""
        Update the least-squares Lagrange multipliers.
        """
        n = self.xopt.size
        if self.mlub + self.mnlub + self.mleq + self.mnleq > 0:
            # Determine the matrix of the least-squares problem. The inequality
            # multipliers corresponding to nonzero constraint values are set to
            # zeros to satisfy the complementary slackness conditions.
            eps = np.finfo(float).eps
            tol = 10. * eps * self.mlub * np.max(np.abs(self.bub), initial=1.)
            rub = np.dot(self.aub, self.xopt) - self.bub
            ilub = np.less_equal(np.abs(rub), tol)
            mlub = np.count_nonzero(ilub)
            rub = self.coptub
            tol = 10. * eps * self.mlub * np.max(np.abs(rub), initial=1.)
            cub_jac = np.empty((self.mnlub, n), dtype=float)
            for i in range(self.mnlub):
                cub_jac[i, :] = self.model_cub_grad(self.xopt, i)
                cub_jac[i, :] -= self.model_cub_hessp(self.xopt, i)
            inlub = np.less_equal(np.abs(rub), tol)
            mnlub = np.count_nonzero(inlub)
            ceq_jac = np.empty((self.mnleq, n), dtype=float)
            for i in range(self.mnleq):
                ceq_jac[i, :] = self.model_ceq_grad(self.xopt, i)
                ceq_jac[i, :] -= self.model_ceq_hessp(self.xopt, i)
            A = np.r_[self.aub[ilub, :], cub_jac[inlub, :], self.aeq, ceq_jac].T

            # Determine the least-squares Lagrange multipliers that have not
            # been fixed by the complementary slackness conditions.
            gopt = self.model_obj_grad(self.xopt)
            lm, _ = nnls(A, -gopt, mlub + mnlub, **kwargs)
            self._lmlub.fill(0.)
            self._lmnlub.fill(0.)
            self._lmlub[ilub] = lm[:mlub]
            self._lmnlub[inlub] = lm[mlub:mlub + mnlub]
            self._lmleq = lm[mlub + mnlub:mlub + mnlub + self.mleq]
            self._lmnleq = lm[mlub + mnlub + self.mleq:]

    def update_penalty_coefficients(self, xnew, fx, cubx, ceqx):
        mx, mmx = self(xnew, fx, cubx, ceqx, True)
        mopt = self(self.xopt, self.fopt, self.coptub, self.copteq)
        if not self.is_model_step and mmx > mopt:
            ksav = self.kopt
            while ksav == self.kopt and mmx > mopt:
                if self._penub > 0.:
                    self._penub *= 2.
                elif self.mlub + self.mnlub > 0:
                    self._penub = 1.
                if self._peneq > 0.:
                    self._peneq *= 2.
                elif self.mleq + self.mnleq > 0:
                    self._peneq = 1.
                mx, mmx = self(xnew, fx, cubx, ceqx, True)
                self.kopt = self.get_best_point()
                mopt = self(self.xopt, self.fopt, self.coptub, self.copteq)
        return mx, mmx, mopt

    def reduce_penalty_coefficients(self):
        fmin = np.min(self.fval)
        fmax = np.max(self.fval)
        if self._penub > 0.:
            resid = np.dot(self.xpt, self.aub.T) - self.bub[np.newaxis, :]
            resid = np.c_[resid, self.cvalub]
            cmin = np.min(resid, axis=1, initial=0.)
            cmax = np.max(resid, axis=1, initial=0.)
            iub = np.less(cmin, 2. * cmax)
            cmin[iub] = np.minimum(0., cmin[iub])
            if np.any(iub):
                denom = np.min(cmax[iub] - cmin[iub])
                self._penub = (fmax - fmin) / denom
            else:
                self._penub = 0.
        if self._peneq > 0.:
            resid = np.dot(self.xpt, self.aeq.T) - self.beq[np.newaxis, :]
            resid = np.c_[resid, self.cvaleq]
            cmin = np.min(resid, axis=1, initial=0.)
            cmax = np.max(resid, axis=1, initial=0.)
            ieq = (cmin < 2. * cmax) | (cmin < .5 * cmax)
            cmax[ieq] = np.maximum(0., cmax[ieq])
            cmin[ieq] = np.minimum(0., cmin[ieq])
            if np.any(ieq):
                denom = np.min(cmax[ieq] - cmin[ieq])
                self._peneq = (fmax - fmin) / denom
            else:
                self._peneq = 0.

    def trust_region_step(self, delta, **kwargs):
        r"""
        Evaluate a Byrd-Omojokun-like trust-region step.

        Notes
        -----
        The trust-region constraint of the tangential subproblem is not centered
        if the normal step is nonzero. To cope with this difficulty, we use the
        result in Equation (15.4.3) of [1]_.

        References
        ----------
        .. [1] A. R. Conn, N. I. M. Gould, and Ph. L. Toint. Trust-Region
        Methods. MPS-SIAM Ser. Optim. Philadelphia, PA, US: SIAM, 2009.
        """
        # Define the tolerances to compare floating-point numbers with zero.
        eps = np.finfo(float).eps
        tol = 1e1 * eps * self.xopt.size

        # Evaluate the normal step of the Byrd-Omojokun approach. The normal
        # step attempts to reduce the violations of the linear constraints
        # subject to the bound constraints and a trust-region constraint. The
        # trust-region radius is shrunk to leave some elbow room to the
        # tangential subproblem for the computations whenever the trust-region
        # subproblem is infeasible.
        delta *= np.sqrt(.5)
        nsf = kwargs.get('nsf', .8)
        mc = self.mlub + self.mnlub + self.mleq + self.mnleq
        aub = np.copy(self.aub)
        bub = np.copy(self.bub)
        for i in range(self.mnlub):
            lhs = self.model_cub_grad(self.xopt, i)
            rhs = np.inner(self.xopt, lhs) - self.coptub[i]
            lhs -= self.model_cub_hessp(self.xopt, i)
            rhs -= .5 * self.model_cub_curv(self.xopt, i)
            aub = np.vstack([aub, lhs])
            bub = np.r_[bub, rhs]
        aeq = np.copy(self.aeq)
        beq = np.copy(self.beq)
        for i in range(self.mnleq):
            lhs = self.model_ceq_grad(self.xopt, i)
            rhs = np.inner(self.xopt, lhs) - self.copteq[i]
            lhs -= self.model_ceq_hessp(self.xopt, i)
            rhs -= .5 * self.model_ceq_curv(self.xopt, i)
            aeq = np.vstack([aeq, lhs])
            beq = np.r_[beq, rhs]
        if mc == 0:
            nstep = np.zeros_like(self.xopt)
            ssq = 0.
        else:
            nstep = cpqp(self.xopt, aub, bub, aeq, beq, self.xl, self.xu,
                         nsf * delta, **kwargs)
            ssq = np.inner(nstep, nstep)

        # Evaluate the tangential step of the trust-region subproblem, and set
        # the global trust-region step. The tangential subproblem is feasible.
        if np.sqrt(ssq) <= tol * max(delta, 1.):
            nstep = np.zeros_like(self.xopt)
            delta *= np.sqrt(2.)
        else:
            delta = np.sqrt(delta ** 2. - ssq)
        xopt = self.xopt + nstep
        gopt = self.model_obj_grad(xopt)
        bub = np.maximum(bub, np.dot(aub, xopt))
        beq = np.dot(aeq, xopt)
        if mc == 0:
            tstep = bvtcg(xopt, gopt, self.model_lag_hessp, (), self.xl,
                          self.xu, delta, **kwargs)
        else:
            tstep = lctcg(xopt, gopt, self.model_lag_hessp, (), aub, bub, aeq,
                          beq, self.xl, self.xu, delta, **kwargs)
        return nstep + tstep

    def model_step(self, delta, **kwargs):
        r"""
        Evaluate a model-improvement step.
        TODO: Give details.
        """
        return self._models.improve_geometry(self._knew, delta, **kwargs)

    def reset_models(self):
        self._models.reset_models()

    def check_models(self, stack_level=2):
        r"""
        Check whether the models satisfy the interpolation conditions.
        """
        self._models.check_models(stack_level)

    def _eval_con(self, con, x):
        if con is not None:
            cx = np.atleast_1d(con(x, *self._args))
            if cx.dtype.kind in np.typecodes['AllInteger']:
                cx = np.asarray(cx, dtype=float)
            if self.disp:
                print(f'{con.__name__}({x}) = {cx}.')
        else:
            cx = np.asarray([], dtype=float)
        return cx


class Models:
    """
    Representation of a model of an optimization problem using quadratic
    functions obtained by underdetermined interpolation.

    The interpolation points may be infeasible with respect to the linear and
    nonlinear constraints, but they always satisfy the bound constraints.

    Notes
    -----
    Given the interpolation set, the freedom bequeathed by the interpolation
    conditions is taken up by minimizing the updates of the Hessian matrices of
    the objective and nonlinear constraint functions in Frobenius norm [1]_.

    References
    ----------
    .. [1] M. J. D. Powell. "Least Frobenius norm updating of quadratic models
       that satisfy interpolation conditions." In: Math. Program. 100 (2004),
       pp. 183--215.
    """

    def __init__(self, fun, x0, xl, xu, Aub, bub, Aeq, beq, cub, ceq, options):
        """
        Construct the initial models of an optimization problem.

        Parameters
        ----------
        fun : callable
            Objective function of the nonlinear optimization problem.

                ``fun(x) -> float``

            where ``x`` is an array with shape (n,).
        x0 : numpy.ndarray, shape (n,)
            Initial guess of the nonlinear optimization problem. It is assumed
            that there is no conflict between the bound constraints and `x0`.
            Hence, the components of the initial guess should either equal the
            bound components or allow the projection of the ball centered at
            `x0` of radius ``options.get('rhobeg)`` onto the coordinates to lie
            entirely inside the bounds.
        xl : numpy.ndarray, shape (n,)
            Lower-bound constraints on the decision variables of the nonlinear
            optimization problem ``x >= xl``.
        xu : numpy.ndarray, shape (n,)
            Upper-bound constraints on the decision variables of the nonlinear
            optimization problem ``x <= xu``.
        Aub : numpy.ndarray, shape (mlub, n)
            Jacobian matrix of the linear inequality constraints of the
            nonlinear optimization problem. Each row of `Aub` stored the
            gradient of a linear inequality constraint.
        bub : numpy.ndarray, shape (mlub,)
            Right-hand side vector of the linear inequality constraints of the
            nonlinear optimization problem ``Aub @ x <= bub``.
        Aeq : numpy.ndarray, shape (mleq, n)
            Jacobian matrix of the linear equality constraints of the nonlinear
            optimization problem. Each row of `Aeq` stored the gradient of a
            linear equality constraint.
        beq : numpy.ndarray, shape (mleq,)
            Right-hand side vector of the linear equality constraints of the
            nonlinear optimization problem ``Aeq @ x = beq``.
        cub : callable
            Nonlinear inequality constraint function of the nonlinear
            optimization problem ``cub(x) <= 0``.

                ``cub(x) -> numpy.ndarray, shape (mnlub,)``

            where ``x`` is an array with shape (n,).
        ceq : callable
            Nonlinear equality constraint function of the nonlinear
            optimization problem ``ceq(x) = 0``.

                ``ceq(x) -> numpy.ndarray, shape (mnleq,)``

            where ``x`` is an array with shape (n,).
        options : dict
        """
        self._xl = xl
        self._xu = xu
        self._Aub = Aub
        self._bub = bub
        self._Aeq = Aeq
        self._beq = beq
        self.shift_constraints(x0)
        n = x0.size
        npt = options.get('npt')
        rhobeg = options.get('rhobeg')
        cub_x0 = cub(x0)
        mnlub = cub_x0.size
        ceq_x0 = ceq(x0)
        mnleq = ceq_x0.size
        self._xpt = np.zeros((npt, n), dtype=float)
        self._fval = np.empty(npt, dtype=float)
        self._rval = np.empty(npt, dtype=float)
        self._cvalub = np.empty((npt, mnlub), dtype=float)
        self._cvaleq = np.empty((npt, mnleq), dtype=float)
        self._bmat = np.zeros((npt + n, n), dtype=float)
        self._zmat = np.zeros((npt, npt - n - 1), dtype=float)
        self._idz = 0
        self._kopt = 0
        stepa = 0.
        stepb = 0.
        for k in range(npt):
            km = k - 1
            kx = km - n

            # Set the displacements from the origin x0 of the calculations of
            # the initial interpolation points in the rows of xpt. It is assumed
            # that there is no conflict between the bounds and x0. Hence, the
            # components of the initial guess should either equal the bound
            # components or allow the projection of the initial trust region
            # onto the components to lie entirely inside the bounds.
            if 1 <= k <= n:
                if abs(self.xu[km]) <= .5 * rhobeg:
                    stepa = -rhobeg
                else:
                    stepa = rhobeg
                self.xpt[k, km] = stepa
            elif n < k <= 2 * n:
                stepa = self.xpt[kx + 1, kx]
                if abs(self.xl[kx]) <= .5 * rhobeg:
                    stepb = min(2. * rhobeg, self.xu[kx])
                elif abs(self.xu[kx]) <= .5 * rhobeg:
                    stepb = max(-2. * rhobeg, self.xl[kx])
                else:
                    stepb = -rhobeg
                self.xpt[k, kx] = stepb
            elif k > 2 * n:
                shift = kx // n
                ipt = kx - shift * n
                jpt = (ipt + shift) % n
                self._xpt[k, ipt] = self.xpt[ipt + 1, ipt]
                self._xpt[k, jpt] = self.xpt[jpt + 1, jpt]

            # Evaluate the objective and the nonlinear constraint functions at
            # the interpolations points and set the residual of each
            # interpolation point in rval.
            self._fval[k] = fun(x0 + self.xpt[k, :])
            if k == 0:
                # The constraints functions have already been evaluated at x0
                # to initialize the shapes of cvalub and cvaleq.
                self._cvalub[0, :] = cub_x0
                self._cvaleq[0, :] = ceq_x0
            else:
                self._cvalub[k, :] = cub(x0 + self.xpt[k, :])
                self._cvaleq[k, :] = ceq(x0 + self.xpt[k, :])
            self._rval[k] = self.resid(k)

            # Set the initial inverse KKT matrix of interpolation. The matrix
            # bmat holds its last n columns, while zmat stored the rank
            # factorization matrix of its leading not submatrix.
            if k <= 2 * n:
                if 1 <= k <= n and npt <= k + n:
                    self._bmat[0, km] = -1 / stepa
                    self._bmat[k, km] = 1 / stepa
                    self._bmat[npt + km, km] = -.5 * rhobeg ** 2.
                elif k > n:
                    self._bmat[0, kx] = -(stepa + stepb) / (stepa * stepb)
                    self._bmat[k, kx] = -.5 / self.xpt[kx + 1, kx]
                    self._bmat[kx + 1, kx] = -self.bmat[0, kx]
                    self._bmat[kx + 1, kx] -= self.bmat[k, kx]
                    self._zmat[0, kx] = np.sqrt(2.) / (stepa * stepb)
                    self._zmat[k, kx] = np.sqrt(.5) / rhobeg ** 2.
                    self._zmat[kx + 1, kx] = -self.zmat[0, kx]
                    self._zmat[kx + 1, kx] -= self.zmat[k, kx]
            else:
                shift = kx // n
                ipt = kx - shift * n
                jpt = (ipt + shift) % n
                self._zmat[0, kx] = 1. / rhobeg ** 2.
                self._zmat[k, kx] = 1. / rhobeg ** 2.
                self._zmat[ipt + 1, kx] = -1. / rhobeg ** 2.
                self._zmat[jpt + 1, kx] = -1. / rhobeg ** 2.

        # Set the initial models of the objective and nonlinear constraint
        # functions. The standard models minimize the updates of their Hessian
        # matrices in Frobenius norm when a point of xpt is modified, while the
        # alternative models minimizes their Hessian matrices in Frobenius norm.
        self._obj = self.new_model(self.fval)
        self._obj_alt = copy.deepcopy(self._obj)
        self._cub = np.empty(mnlub, dtype=Quadratic)
        self._cub_alt = np.empty(mnlub, dtype=Quadratic)
        for i in range(mnlub):
            self._cub[i] = self.new_model(self.cvalub[:, i])
            self._cub_alt[i] = copy.deepcopy(self._cub[i])
        self._ceq = np.empty(mnleq, dtype=Quadratic)
        self._ceq_alt = np.empty(mnleq, dtype=Quadratic)
        for i in range(mnleq):
            self._ceq[i] = self.new_model(self.cvaleq[:, i])
            self._ceq_alt[i] = copy.deepcopy(self._ceq[i])

    @property
    def xl(self):
        """
        Get the lower-bound constraints on the decision variables.

        Returns
        -------
        numpy.ndarray, shape (n,)
            Lower-bound constraints on the decision variables.
        """
        return self._xl

    @property
    def xu(self):
        """
        Get the upper-bound constraints on the decision variables.

        Returns
        -------
        numpy.ndarray, shape (n,)
            Upper-bound constraints on the decision variables.
        """
        return self._xu

    @property
    def aub(self):
        """
        Get the Jacobian matrix of the linear inequality constraints.

        Returns
        -------
        numpy.ndarray, shape (mlub, n)
            Jacobian matrix of the linear inequality constraints.
        """
        return self._Aub

    @property
    def bub(self):
        """
        Get the right-hand side vector of the linear inequality constraints.

        Returns
        -------
        numpy.ndarray, shape (mlub,)
            Right-hand side vector of the linear inequality constraints.
        """
        return self._bub

    @property
    def mlub(self):
        """
        Get the number of the linear inequality constraints.

        Returns
        -------
        int
            Number of the linear inequality constraints.
        """
        return self.bub.size

    @property
    def aeq(self):
        """
        Get the Jacobian matrix of the linear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (mleq, n)
            Jacobian matrix of the linear equality constraints.
        """
        return self._Aeq

    @property
    def beq(self):
        """
        Get the right-hand side vector of the linear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (mleq,)
            Right-hand side vector of the linear equality constraints.
        """
        return self._beq

    @property
    def mleq(self):
        """
        Get the number of the linear equality constraints.

        Returns
        -------
        int
            Number of the linear equality constraints.
        """
        return self.beq.size

    @property
    def xpt(self):
        """
        Get the displacements of the interpolation points from the origin.

        Returns
        -------
        numpy.ndarray, shape (npt, n)
            Displacements of the interpolation points from the origin. Each row
            of the returned matrix stored the displacements of an interpolation
            point from the origin of the calculations.
        """
        return self._xpt

    @property
    def fval(self):
        """
        Get the evaluations of the objective function of the nonlinear
        optimization problem at the interpolation points.

        Returns
        -------
        numpy.ndarray, shape (npt,)
            Evaluations of the objective function of the nonlinear optimization
            problem at the interpolation points.
        """
        return self._fval

    @property
    def rval(self):
        """
        Get the residuals associated with the constraints of the nonlinear
        optimization problem at the interpolation points.

        Returns
        -------
        numpy.ndarray, shape (npt,)
            Residuals associated with the constraints of the nonlinear
            optimization problem at the interpolation points.
        """
        return self._rval

    @property
    def cvalub(self):
        """
        Get the evaluations of the nonlinear inequality constraint function of
        the nonlinear optimization problem at the interpolation points.

        Returns
        -------
        numpy.ndarray, shape (npt, mnlub)
            Evaluations of the nonlinear inequality constraint function of the
            nonlinear optimization problem at the interpolation points.
        """
        return self._cvalub

    @property
    def mnlub(self):
        """
        Get the number of the nonlinear inequality constraints.

        Returns
        -------
        int
            Number of the nonlinear inequality constraints.
        """
        return self.cvalub.shape[1]

    @property
    def cvaleq(self):
        """
        Get the evaluations of the nonlinear equality constraint function of the
        nonlinear optimization problem at the interpolation points.

        Returns
        -------
        numpy.ndarray, shape (npt, mnleq)
            Evaluations of the nonlinear equality constraint function of the
            nonlinear optimization problem at the interpolation points.
        """
        return self._cvaleq

    @property
    def mnleq(self):
        """
        Get the number of the nonlinear equality constraints.

        Returns
        -------
        int
            Number of the nonlinear equality constraints.
        """
        return self.cvaleq.shape[1]

    @property
    def bmat(self):
        """
        Get the last ``n`` columns of the inverse KKT matrix of interpolation.

        Returns
        -------
        numpy.ndarray, shape (npt + n, n)
            Last ``n`` columns of the inverse KKT matrix of interpolation.
        """
        return self._bmat

    @property
    def zmat(self):
        """
        Get the rank factorization matrix of the leading ``npt`` submatrix of
        the inverse KKT matrix of interpolation.

        Returns
        -------
        numpy.ndarray, shape (npt, npt - n - 1)
            Rank factorization matrix of the leading ``npt`` submatrix of the
            inverse KKT matrix of interpolation.
        """
        return self._zmat

    @property
    def idz(self):
        """
        Get the number of nonpositive eigenvalues of the leading ``npt``
        submatrix of the inverse KKT matrix of interpolation.

        Returns
        -------
        int
            Number of nonpositive eigenvalues of the leading ``npt`` submatrix
            of the inverse KKT matrix of interpolation.

        Notes
        -----
        Although the theoretical number of nonpositive eigenvalues of this
        matrix is always 0, it is designed to tackle numerical difficulties
        caused by ill-conditioned problems.
        """
        return self._idz

    @property
    def kopt(self):
        """
        Get the index of the interpolation point around which the Taylor
        expansion of the quadratic models are defined.

        Returns
        -------
        int
            Index of the interpolation point around which the Taylor expansion
            of the quadratic models are defined.
        """
        return self._kopt

    @kopt.setter
    def kopt(self, knew):
        """
        Set the index of the interpolation point around which the Taylor
        expansion of the quadratic models are defined.

        Parameters
        ----------
        knew : int
            New index of the interpolation point around which the Taylor
            expansion of the quadratic models is to be defined.
        """
        if self._kopt != knew:
            step = self.xpt[knew, :] - self.xopt
            self._obj.shift_expansion_point(step, self.xpt)
            self._obj_alt.shift_expansion_point(step, self.xpt)
            for i in range(self.mnlub):
                self._cub[i].shift_expansion_point(step, self.xpt)
                self._cub_alt[i].shift_expansion_point(step, self.xpt)
            for i in range(self.mnleq):
                self._ceq[i].shift_expansion_point(step, self.xpt)
                self._ceq_alt[i].shift_expansion_point(step, self.xpt)
            self._kopt = knew

    @property
    def xopt(self):
        """
        Get the interpolation point around which the Taylor expansion of the
        quadratic models are defined.

        Returns
        -------
        numpy.ndarray, shape (n,)
            Interpolation point around which the Taylor expansion of the
            quadratic models are defined.
        """
        return self.xpt[self.kopt, :]

    @property
    def fopt(self):
        """
        Get the evaluation of the objective function of the nonlinear
        optimization problem at the interpolation point around which the Taylor
        expansion of the quadratic models are defined.

        Returns
        -------
        float
            Evaluation of the objective function of the nonlinear optimization
            problem at the interpolation point around which the Taylor
            expansion of the quadratic models are defined.
        """
        return self.fval[self.kopt]

    @property
    def ropt(self):
        """
        Get the residual associated with the constraints of the nonlinear
        optimization problem at the interpolation point around which the Taylor
        expansion of the quadratic models are defined.

        Returns
        -------
        float
            Residual associated with the constraints of the nonlinear
            optimization problem at the interpolation point around which the
            Taylor expansion of the quadratic models are defined.
        """
        return self.rval[self.kopt]

    @property
    def coptub(self):
        """
        Get the evaluation of the nonlinear inequality constraint function of
        the nonlinear optimization problem at the interpolation point around
        which the Taylor expansion of the quadratic models are defined.

        Returns
        -------
        numpy.ndarray, shape (mnlub,)
            Evaluation of the nonlinear inequality constraint function of the
            nonlinear optimization problem at the interpolation point around
            which the Taylor expansion of the quadratic models are defined.
        """
        return self.cvalub[self.kopt, :]

    @property
    def copteq(self):
        """
        Get the evaluation of the nonlinear equality constraint function of the
        nonlinear optimization problem at the interpolation point around which
        the Taylor expansion of the quadratic models are defined.

        Returns
        -------
        numpy.ndarray, shape (mnleq,)
            Evaluation of the nonlinear equality constraint function of the
            nonlinear optimization problem at the interpolation point around
            which the Taylor expansion of the quadratic models are defined.
        """
        return self.cvaleq[self.kopt, :]

    @property
    def type(self):
        """
        Get the type of the nonlinear optimization problem.

        It follows the CUTEst classification scheme for the constraint types
        (see https://www.cuter.rl.ac.uk/Problems/classification.shtml).

        Returns
        -------
        {'U', 'X', 'B', 'L', 'O'}
            Type of the nonlinear optimization problem:

                #. 'U' : the problem is unconstrained.
                #. 'X' : the problem only constraints are fixed variables.
                #. 'B' : the problem only constraints are bounds constraints.
                #. 'L' : the problem constraints are linear.
                #. 'O' : the problem constraints general.
        """
        n = self.xpt.shape[1]
        eps = np.finfo(float).eps
        if self.mnlub + self.mnleq > 0:
            return 'O'
        elif self.mlub + self.mleq > 0:
            return 'L'
        elif np.all(self.xl == -np.inf) and np.all(self.xu == np.inf):
            return 'U'
        elif np.all(self.xu - self.xl <= 10. * eps * n * np.abs(self.xu)):
            return 'X'
        else:
            return 'B'

    def obj(self, x):
        """
        Evaluate the objective function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.

        Returns
        -------
        float:
            Value of the objective function of the model at `x`.
        """
        return self.fopt + self._obj(x, self.xpt, self.kopt)

    def obj_grad(self, x):
        """
        Evaluate the gradient of the objective function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the objective function of the model at `x`.
        """
        return self._obj.grad(x, self.xpt, self.kopt)

    def obj_hess(self):
        """
        Evaluate the Hessian matrix of the objective function of the model.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the objective function of the model.
        """
        return self._obj.hess(self.xpt)

    def obj_hessp(self, x):
        """
        Evaluate the product of the Hessian matrix of the objective function of
        the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the objective function
            of the model with the vector `x`.
        """
        return self._obj.hessp(x, self.xpt)

    def obj_curv(self, x):
        """
        Evaluate the curvature of the objective function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.

        Returns
        -------
        float
            Curvature of the objective function of the model at `x`.
        """
        return self._obj.curv(x, self.xpt)

    def obj_alt(self, x):
        """
        Evaluate the alternative objective function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.

        Returns
        -------
        float:
            Value of the alternative objective function of the model at `x`.
        """
        return self.fopt + self._obj_alt(x, self.xpt, self.kopt)

    def obj_alt_grad(self, x):
        """
        Evaluate the gradient of the alternative objective function of the
        model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the alternative objective function of the model at `x`.
        """
        return self._obj_alt.grad(x, self.xpt, self.kopt)

    def obj_alt_hess(self):
        """
        Evaluate the Hessian matrix of the alternative objective function of the
        model.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the alternative objective function of the model.
        """
        return self._obj_alt.hess(self.xpt)

    def obj_alt_hessp(self, x):
        """
        Evaluate the product of the Hessian matrix of the alternative objective
        function of the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the alternative
            objective function of the model with the vector `x`.
        """
        return self._obj_alt.hessp(x, self.xpt)

    def obj_alt_curv(self, x):
        """
        Evaluate the curvature of the alternative objective function of the
        model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.

        Returns
        -------
        float
            Curvature of the alternative objective function of the model at `x`.
        """
        return self._obj_alt.curv(x, self.xpt)

    def cub(self, x, i):
        """
        Evaluate an inequality constraint function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        float:
            Value of the `i`-th inequality constraint function of the model at
            `x`.
        """
        return self.coptub[i] + self._cub[i](x, self.xpt, self.kopt)

    def cub_grad(self, x, i):
        """
        Evaluate the gradient of an inequality constraint function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the `i`-th inequality constraint function of the model
            at `x`.
        """
        return self._cub[i].grad(x, self.xpt, self.kopt)

    def cub_hess(self, i):
        """
        Evaluate the Hessian matrix of an inequality constraint function of the
        model.

        Parameters
        ----------
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the `i`-th inequality constraint function of the
            model.
        """
        return self._cub[i].hess(self.xpt)

    def cub_hessp(self, x, i):
        """
        Evaluate the product of the Hessian matrix of an inequality constraint
        function of the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the `i`-th inequality
            constraint function of the model with the vector `x`.
        """
        return self._cub[i].hessp(x, self.xpt)

    def cub_curv(self, x, i):
        """
        Evaluate the curvature of an inequality constraint function of the
        model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        float
            Curvature of the `i`-th inequality constraint function of the model
            at `x`.
        """
        return self._cub[i].curv(x, self.xpt)

    def cub_alt(self, x, i):
        """
        Evaluate an alternative inequality constraint function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        float:
            Value of the `i`-th alternative inequality constraint function of
            the model at `x`.
        """
        return self.coptub[i] + self._cub_alt[i](x, self.xpt, self.kopt)

    def cub_alt_grad(self, x, i):
        """
        Evaluate the gradient of an alternative inequality constraint function
        of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the `i`-th alternative inequality constraint function of
            the model at `x`.
        """
        return self._cub_alt[i].grad(x, self.xpt, self.kopt)

    def cub_alt_hess(self, i):
        """
        Evaluate the Hessian matrix of an alternative inequality constraint
        function of the model.

        Parameters
        ----------
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the `i`-th alternative inequality constraint
            function of the model.
        """
        return self._cub_alt[i].hess(self.xpt)

    def cub_alt_hessp(self, x, i):
        """
        Evaluate the product of the Hessian matrix of an alternative inequality
        constraint function of the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the `i`-th alternative
            inequality constraint function of the model with the vector `x`.
        """
        return self._cub_alt[i].hessp(x, self.xpt)

    def cub_alt_curv(self, x, i):
        """
        Evaluate the curvature of an alternative inequality constraint function
        of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.
        i : int
            Index of the inequality constraint to be considered.

        Returns
        -------
        float
            Curvature of the `i`-th alternative inequality constraint function
            of the model at `x`.
        """
        return self._cub_alt[i].curv(x, self.xpt)

    def ceq(self, x, i):
        """
        Evaluate an equality constraint function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        float:
            Value of the `i`-th equality constraint function of the model at
            `x`.
        """
        return self.copteq[i] + self._ceq[i](x, self.xpt, self.kopt)

    def ceq_grad(self, x, i):
        """
        Evaluate the gradient of an equality constraint function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the `i`-th equality constraint function of the model at
            `x`.
        """
        return self._ceq[i].grad(x, self.xpt, self.kopt)

    def ceq_hess(self, i):
        """
        Evaluate the Hessian matrix of an equality constraint function of the
        model.

        Parameters
        ----------
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the `i`-th equality constraint function of the
            model.
        """
        return self._ceq[i].hess(self.xpt)

    def ceq_hessp(self, x, i):
        """
        Evaluate the product of the Hessian matrix of an equality constraint
        function of the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the `i`-th equality
            constraint function of the model with the vector `x`.
        """
        return self._ceq[i].hessp(x, self.xpt)

    def ceq_curv(self, x, i):
        """
        Evaluate the curvature of an equality constraint function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        float
            Curvature of the `i`-th equality constraint function of the model at
            `x`.
        """
        return self._ceq[i].curv(x, self.xpt)

    def ceq_alt(self, x, i):
        """
        Evaluate an alternative equality constraint function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        float:
            Value of the `i`-th alternative equality constraint function of the
            model at `x`.
        """
        return self.copteq[i] + self._ceq_alt[i](x, self.xpt, self.kopt)

    def ceq_alt_grad(self, x, i):
        """
        Evaluate the gradient of an alternative equality constraint function of
        the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the `i`-th alternative equality constraint function of
            the model at `x`.
        """
        return self._ceq_alt[i].grad(x, self.xpt, self.kopt)

    def ceq_alt_hess(self, i):
        """
        Evaluate the Hessian matrix of an alternative equality constraint
        function of the model.

        Parameters
        ----------
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the `i`-th alternative equality constraint
            function of the model.
        """
        return self._ceq_alt[i].hess(self.xpt)

    def ceq_alt_hessp(self, x, i):
        """
        Evaluate the product of the Hessian matrix of an alternative equality
        constraint function of the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the `i`-th alternative
            equality constraint function of the model with the vector `x`.
        """
        return self._ceq_alt[i].hessp(x, self.xpt)

    def ceq_alt_curv(self, x, i):
        """
        Evaluate the curvature of an alternative equality constraint function of
        the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.
        i : int
            Index of the equality constraint to be considered.

        Returns
        -------
        float
            Curvature of the `i`-th alternative equality constraint function of
            the model at `x`.
        """
        return self._ceq_alt[i].curv(x, self.xpt)

    def lag(self, x, lmlub, lmleq, lmnlub, lmnleq):
        """
        Evaluate the Lagrangian function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        lmlub : numpy.ndarray, shape (mlub,)
            Lagrange multipliers associated with the linear inequality
            constraints.
        lmleq : numpy.ndarray, shape (mleq,)
            Lagrange multipliers associated with the linear equality
            constraints.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        float:
            Value of the Lagrangian function of the model at `x`.
        """
        lx = self.obj(x)
        lx += np.inner(lmlub, np.dot(self.aub, x) - self.bub)
        lx += np.inner(lmleq, np.dot(self.aeq, x) - self.beq)
        for i in range(self.mnlub):
            lx += lmnlub[i] * self.cub(x, i)
        for i in range(self.mnleq):
            lx += lmnleq[i] * self.ceq(x, i)
        return lx

    def lag_grad(self, x, lmlub, lmleq, lmnlub, lmnleq):
        """
        Evaluate the gradient of Lagrangian function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.
        lmlub : numpy.ndarray, shape (mlub,)
            Lagrange multipliers associated with the linear inequality
            constraints.
        lmleq : numpy.ndarray, shape (mleq,)
            Lagrange multipliers associated with the linear equality
            constraints.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the Lagrangian function of the model at `x`.
        """
        gx = self.obj_grad(x)
        gx += np.dot(self.aub.T, lmlub)
        gx += np.dot(self.aeq.T, lmleq)
        for i in range(self.mnlub):
            gx += lmnlub[i] * self.cub_grad(x, i)
        for i in range(self.mnleq):
            gx += lmnleq[i] * self.ceq_grad(x, i)
        return gx

    def lag_hess(self, lmnlub, lmnleq):
        """
        Evaluate the Hessian matrix of the Lagrangian function of the model.

        Parameters
        ----------
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the Lagrangian function of the model.
        """
        hx = self.obj_hess()
        for i in range(self.mnlub):
            hx += lmnlub[i] * self.cub_hess(i)
        for i in range(self.mnleq):
            hx += lmnleq[i] * self.ceq_hess(i)
        return hx

    def lag_hessp(self, x, lmnlub, lmnleq):
        """
        Evaluate the product of the Hessian matrix of the Lagrangian function of
        the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the Lagrangian
            function of the model with the vector `x`.
        """
        hx = self.obj_hessp(x)
        for i in range(self.mnlub):
            hx += lmnlub[i] * self.cub_hessp(x, i)
        for i in range(self.mnleq):
            hx += lmnleq[i] * self.ceq_hessp(x, i)
        return hx

    def lag_curv(self, x, lmnlub, lmnleq):
        """
        Evaluate the curvature of the Lagrangian function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        float
            Curvature of the Lagrangian function of the model at `x`.
        """
        cx = self.obj_curv(x)
        for i in range(self.mnlub):
            cx += lmnlub[i] * self.cub_curv(x, i)
        for i in range(self.mnleq):
            cx += lmnleq[i] * self.ceq_curv(x, i)
        return cx

    def lag_alt(self, x, lmlub, lmleq, lmnlub, lmnleq):
        """
        Evaluate the alternative Lagrangian function of the model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        lmlub : numpy.ndarray, shape (mlub,)
            Lagrange multipliers associated with the linear inequality
            constraints.
        lmleq : numpy.ndarray, shape (mleq,)
            Lagrange multipliers associated with the linear equality
            constraints.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        float:
            Value of the alternative Lagrangian function of the model at `x`.
        """
        lx = self.obj_alt(x)
        lx += np.inner(lmlub, np.dot(self.aub, x) - self.bub)
        lx += np.inner(lmleq, np.dot(self.aeq, x) - self.beq)
        for i in range(self.mnlub):
            lx += lmnlub[i] * self.cub_alt(x, i)
        for i in range(self.mnleq):
            lx += lmnleq[i] * self.ceq_alt(x, i)
        return lx

    def lag_alt_grad(self, x, lmlub, lmleq, lmnlub, lmnleq):
        """
        Evaluate the gradient of the alternative Lagrangian function of the
        model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the gradient of the quadratic function is to be
            evaluated.
        lmlub : numpy.ndarray, shape (mlub,)
            Lagrange multipliers associated with the linear inequality
            constraints.
        lmleq : numpy.ndarray, shape (mleq,)
            Lagrange multipliers associated with the linear equality
            constraints.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Gradient of the alternative Lagrangian function of the model at `x`.
        """
        gx = self.obj_alt_grad(x)
        gx += np.dot(self.aub.T, lmlub)
        gx += np.dot(self.aeq.T, lmleq)
        for i in range(self.mnlub):
            gx += lmnlub[i] * self.cub_alt_grad(x, i)
        for i in range(self.mnleq):
            gx += lmnleq[i] * self.ceq_alt_grad(x, i)
        return gx

    def lag_alt_hess(self, lmnlub, lmnleq):
        """
        Evaluate the Hessian matrix of the alternative Lagrangian function of
        the model.

        Parameters
        ----------
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (n, n):
            Hessian matrix of the alternative Lagrangian function of the model.
        """
        hx = self.obj_alt_hess()
        for i in range(self.mnlub):
            hx += lmnlub[i] * self.cub_alt_hess(i)
        for i in range(self.mnleq):
            hx += lmnleq[i] * self.ceq_alt_hess(i)
        return hx

    def lag_alt_hessp(self, x, lmnlub, lmnleq):
        """
        Evaluate the product of the Hessian matrix of the alternative Lagrangian
        function of the model with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        numpy.ndarray, shape (n,):
            Value of the product of the Hessian matrix of the alternative
            Lagrangian function of the model with the vector `x`.
        """
        hx = self.obj_alt_hessp(x)
        for i in range(self.mnlub):
            hx += lmnlub[i] * self.cub_alt_hessp(x, i)
        for i in range(self.mnleq):
            hx += lmnleq[i] * self.ceq_alt_hessp(x, i)
        return hx

    def lag_alt_curv(self, x, lmnlub, lmnleq):
        """
        Evaluate the curvature of the alternative Lagrangian function of the
        model.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.
        lmnlub : numpy.ndarray, shape (mnlub,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear inequality constraints.
        lmnleq : numpy.ndarray, shape (mnleq,)
            Lagrange multipliers associated with the quadratic models of the
            nonlinear equality constraints.

        Returns
        -------
        float
            Curvature of the alternative Lagrangian function of the model at
            `x`.
        """
        cx = self.obj_alt_curv(x)
        for i in range(self.mnlub):
            cx += lmnlub[i] * self.cub_alt_curv(x, i)
        for i in range(self.mnleq):
            cx += lmnleq[i] * self.ceq_alt_curv(x, i)
        return cx

    def shift_constraints(self, x):
        """
        Shift the bound constraints and the right-hand sides of the linear
        inequality and equality constraints.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Coordinates of the shift to be performed.
        """
        self._xl -= x
        self._xu -= x
        self._bub -= np.dot(self.aub, x)
        self._beq -= np.dot(self.aeq, x)

    def shift_origin(self):
        """
        Update the models of the nonlinear optimization problem when the origin
        of the calculations is modified.

        Notes
        -----
        Given ``xbase`` the previous origin of the calculations, it is assumed
        that it is shifted to ``xbase + self.xopt``.
        """
        xopt = np.copy(self.xopt)
        npt, n = self.xpt.shape
        xoptsq = np.inner(xopt, xopt)

        # Make the changes to bmat that do not depend on zmat.
        qoptsq = .25 * xoptsq
        updt = np.dot(self.xpt, xopt) - .5 * xoptsq
        hxpt = self.xpt - .5 * xopt[np.newaxis, :]
        for k in range(npt):
            step = updt[k] * hxpt[k, :] + qoptsq * xopt
            temp = np.outer(self.bmat[k, :], step)
            self._bmat[npt:, :] += temp + temp.T

        # Revise bmat to incorporate the changes that depend on zmat.
        temp = qoptsq * np.outer(xopt, np.sum(self.zmat, axis=0))
        temp += np.matmul(hxpt.T, self.zmat * updt[:, np.newaxis])
        for k in range(self._idz):
            self._bmat[:npt, :] -= np.outer(self.zmat[:, k], temp[:, k])
            self._bmat[npt:, :] -= np.outer(temp[:, k], temp[:, k])
        for k in range(self._idz, npt - n - 1):
            self._bmat[:npt, :] += np.outer(self.zmat[:, k], temp[:, k])
            self._bmat[npt:, :] += np.outer(temp[:, k], temp[:, k])

        # Complete the shift by updating the quadratic models, the bound
        # constraints, the right-hand side of the linear inequality and equality
        # constraints, and the interpolation points.
        self._obj.shift_interpolation_points(self.xpt, self.kopt)
        self._obj_alt.shift_interpolation_points(self.xpt, self.kopt)
        for i in range(self.mnlub):
            self._cub[i].shift_interpolation_points(self.xpt, self.kopt)
            self._cub_alt[i].shift_interpolation_points(self.xpt, self.kopt)
        for i in range(self.mnleq):
            self._ceq[i].shift_interpolation_points(self.xpt, self.kopt)
            self._ceq_alt[i].shift_interpolation_points(self.xpt, self.kopt)
        self.shift_constraints(xopt)
        self._xpt -= xopt[np.newaxis, :]

    def update(self, step, fx, cubx, ceqx, knew=None):
        """
        Update the models of the nonlinear optimization problem when a point of
        the interpolation set is modified.

        Parameters
        ----------
        step : numpy.ndarray, shape (n,)
            Displacement from ``self.xopt`` of the point to replace an
            interpolation point.
        fx : float
            Value of the objective function at ``self.xopt + step``.
        cubx : numpy.ndarray, shape (mnlub,)
            Value of the nonlinear inequality constraint function at
            ``self.xopt + step``.
        ceqx : numpy.ndarray, shape (mnleq,)
            Value of the nonlinear equality constraint function at
            ``self.xopt + step``.
        knew : int, optional
            Index of the interpolation point to be removed. It is automatically
            chosen if it is not provided.

        Returns
        -------
        int
            Index of the interpolation point that has been replaced.

        Raises
        ------
        ZeroDivisionError
            The denominator of the updating formula is zero.

        Notes
        -----
        When the index `knew` of the interpolation point to be removed is not
        provided, it is chosen by the method to maximize the product absolute
        value of the denominator in Equation (2.12) of [1]_ with the quartic
        power of the distance between the point and ``self.xopt``.

        References
        ----------
        .. [1] M. J. D. Powell. "On updating the inverse of a KKT matrix." In:
           Numerical Linear Algebra and Optimization. Ed. by Y. Yuan. Beijing,
           CN: Science Press, 2004, pp. 56--78.
        """
        npt, n = self.xpt.shape
        tiny = np.finfo(float).tiny

        # Evaluate the Lagrange polynomials related to the interpolation points
        # and the real parameter beta given in Equation (2.13) of Powell (2004).
        beta, vlag = self._beta(step)

        # Select the index of the interpolation point to be deleted.
        if knew is None:
            knew = self._get_point_to_remove(beta, vlag)

        # Put zeros in the knew-th row of zmat by applying a sequence of Givens
        # rotations. The remaining updates are performed below.
        jdz = 0
        for j in range(1, npt - n - 1):
            if j == self.idz:
                jdz = self.idz
            elif abs(self.zmat[knew, j]) > 0.:
                cval = self.zmat[knew, jdz]
                sval = self.zmat[knew, j]
                givens(self._zmat, cval, sval, j, jdz, 1)
                self._zmat[knew, j] = 0.

        # Evaluate the denominator in Equation (2.12) of Powell (2004).
        scala = self.zmat[knew, 0] if self.idz == 0 else -self.zmat[knew, 0]
        scalb = 0. if jdz == 0 else self.zmat[knew, jdz]
        omega = scala * self.zmat[:, 0] + scalb * self.zmat[:, jdz]
        alpha = omega[knew]
        tau = vlag[knew]
        sigma = alpha * beta + tau ** 2.
        vlag[knew] -= 1.
        bmax = np.max(np.abs(self.bmat), initial=1.)
        zmax = np.max(np.abs(self.zmat), initial=1.)
        if abs(sigma) < tiny * max(bmax, zmax):
            # The denominator of the updating formula is too small to safely
            # divide the coefficients of the KKT matrix of interpolation.
            # Theoretically, the value of abs(sigma) is always positive, and
            # becomes small only for ill-conditioned problems.
            raise ZeroDivisionError

        # Complete the update of the matrix zmat. The boolean variable reduce
        # indicates whether the number of nonpositive eigenvalues of the leading
        # npt submatrix of the inverse KKT matrix of interpolation in self._idz
        # must be decreased by one.
        reduce = False
        hval = np.sqrt(abs(sigma))
        if jdz == 0:
            scala = tau / hval
            scalb = self.zmat[knew, 0] / hval
            self._zmat[:, 0] = scala * self.zmat[:, 0] - scalb * vlag[:npt]
            if sigma < 0.:
                if self.idz == 0:
                    self._idz = 1
                else:
                    reduce = True
        else:
            kdz = jdz if beta >= 0. else 0
            jdz -= kdz
            tempa = self.zmat[knew, jdz] * beta / sigma
            tempb = self.zmat[knew, jdz] * tau / sigma
            temp = self.zmat[knew, kdz]
            scala = 1. / np.sqrt(abs(beta) * temp ** 2. + tau ** 2.)
            scalb = scala * hval
            self._zmat[:, kdz] = tau * self.zmat[:, kdz] - temp * vlag[:npt]
            self._zmat[:, kdz] *= scala
            self._zmat[:, jdz] -= tempa * omega + tempb * vlag[:npt]
            self._zmat[:, jdz] *= scalb
            if sigma <= 0.:
                if beta < 0.:
                    self._idz += 1
                else:
                    reduce = True
        if reduce:
            self._idz -= 1
            self._zmat[:, [0, self.idz]] = self.zmat[:, [self.idz, 0]]

        # Update accordingly bmat. The copy below is crucial, as the slicing
        # would otherwise return a view of the knew-th row of bmat only.
        bsav = np.copy(self.bmat[knew, :])
        for j in range(n):
            cosv = (alpha * vlag[npt + j] - tau * bsav[j]) / sigma
            sinv = (tau * vlag[npt + j] + beta * bsav[j]) / sigma
            self._bmat[:npt, j] += cosv * vlag[:npt] - sinv * omega
            self._bmat[npt:npt + j + 1, j] += cosv * vlag[npt:npt + j + 1]
            self._bmat[npt:npt + j + 1, j] -= sinv * bsav[:j + 1]
            self._bmat[npt + j, :j + 1] = self.bmat[npt:npt + j + 1, j]

        # Update finally the evaluations of the objective function, the
        # nonlinear inequality constraint function, and the nonlinear equality
        # constraint function, the residuals of the interpolation points, the
        # interpolation points, and the models of the problem.
        xnew = self.xopt + step
        xold = np.copy(self.xpt[knew, :])
        dfx = fx - self.obj(xnew)
        self._fval[knew] = fx
        dcubx = np.empty(self.mnlub, dtype=float)
        for i in range(self.mnlub):
            dcubx[i] = cubx[i] - self.cub(xnew, i)
        self._cvalub[knew, :] = cubx
        dceqx = np.empty(self.mnleq, dtype=float)
        for i in range(self.mnleq):
            dceqx[i] = ceqx[i] - self.ceq(xnew, i)
        self._cvaleq[knew, :] = ceqx
        self._rval[knew] = self.resid(knew)
        self._xpt[knew, :] = xnew
        self._obj.update(self.xpt, self.kopt, xold, self.bmat, self.zmat,
                         self.idz, knew, dfx)
        self._obj_alt = self.new_model(self.fval)
        for i in range(self.mnlub):
            self._cub[i].update(self.xpt, self.kopt, xold, self.bmat, self.zmat,
                                self.idz, knew, dcubx[i])
            self._cub_alt[i] = self.new_model(self.cvalub[:, i])
        for i in range(self.mnleq):
            self._ceq[i].update(self.xpt, self.kopt, xold, self.bmat, self.zmat,
                                self.idz, knew, dceqx[i])
            self._ceq_alt[i] = self.new_model(self.cvaleq[:, i])
        return knew

    def new_model(self, fval):
        """
        Generate a model obtained by underdetermined interpolation.

        The freedom bequeathed by the interpolation conditions defined by `fval`
        is taken up by minimizing the Hessian matrix of the quadratic function
        in Frobenius norm.

        Parameters
        ----------
        fval : int or numpy.ndarray, shape (npt,)
            Evaluations associated with the interpolation points. An integer
            value represents the ``npt``-dimensional vector whose components are
            all zero, except the `fval`-th one whose value is one. Hence,
            passing an integer value construct the `fval`-th Lagrange polynomial
            associated with the interpolation points.

        Returns
        -------
        Quadratic
            The quadratic model that satisfy the interpolation conditions
            defined by `fval`, whose Hessian matrix is least in Frobenius norm.
        """
        model = Quadratic(self.bmat, self.zmat, self.idz, fval)
        model.shift_expansion_point(self.xopt, self.xpt)
        return model

    def reset_models(self):
        """
        Set the standard models of the objective function, the nonlinear
        inequality constraint function, and the nonlinear equality constraint
        function to the ones whose Hessian matrices are least in Frobenius norm.
        """
        self._obj = copy.deepcopy(self._obj_alt)
        self._cub = copy.deepcopy(self._cub_alt)
        self._ceq = copy.deepcopy(self._ceq_alt)

    def improve_geometry(self, klag, delta, **kwargs):
        """
        Estimate a step from ``self.xopt`` that aims at improving the geometry
        of the interpolation set.

        Two alternative steps are computed.

            1. The first alternative step is selected on the lines that join
               ``self.xopt`` to the other interpolation points that maximize a
               lower bound on the denominator of the updating formula.
            2. The second alternative is a constrained Cauchy step.

        Among the two alternative steps, the method selects the one that leads
        to the greatest denominator of the updating formula.

        Parameters
        ----------
        klag : int
            Index of the interpolation point that is to be replaced.
        delta : float
            Upper bound on the length of the step.

        Returns
        -------
        numpy.ndarray, shape (n,)
            Step from ``self.xopt`` that aims at improving the geometry of the
            interpolation set.

        Other Parameters
        ----------------
        bdtol : float, optional
            Tolerance for comparisons on the bound constraints (the default is
            ``10 * eps * n * max(1, max(abs(xl)), max(abs(xu)))``, where the
            values of `xl` and `xu` evolve to include the shift of the origin).
        """
        # Define the tolerances to compare floating-point numbers with zero.
        npt = self.xpt.shape[0]
        eps = np.finfo(float).eps
        tol = 10. * eps * npt

        # Determine the klag-th Lagrange polynomial. It is the quadratic
        # function whose value is zero at each interpolation point, except at
        # the klag-th one, hose value is one. The freedom bequeathed by these
        # interpolation conditions is taken up by minimizing the Hessian matrix
        # of the quadratic function is Frobenius norm.
        lag = self.new_model(klag)

        # Determine a point on a line between ``self.xopt`` and another
        # interpolation points, chosen to maximize the absolute value of the
        # klag-th Lagrange polynomial, which is a lower bound on the denominator
        # of the updating formula.
        omega = omega_product(self.zmat, self.idz, klag)
        alpha = omega[klag]
        glag = lag.grad(self.xopt, self.xpt, self.kopt)
        step = bvlag(self.xpt, self.kopt, klag, glag, self.xl, self.xu, delta,
                     alpha, **kwargs)

        # Evaluate the constrained Cauchy step from the optimal point of the
        # absolute value of the klag-th Lagrange polynomial.
        salt, cauchy = bvcs(self.xpt, self.kopt, glag, lag.curv, (self.xpt,),
                            self.xl, self.xu, delta, **kwargs)

        # Among the two computed alternative points, we select the one that
        # leads to the greatest denominator of the updating formula.
        beta, vlag = self._beta(step)
        sigma = vlag[klag] ** 2. + alpha * beta
        if sigma < cauchy and cauchy > tol * max(1, abs(sigma)):
            step = salt
        return step

    def resid(self, x, cubx=None, ceqx=None):
        """
        Evaluate the residual associated with the constraints of the nonlinear
        optimization problem.

        Parameters
        ----------
        x : int or numpy.ndarray, shape (n,)
            Point at which the residual is to be evaluated. An integer value
            represents the `x`-th interpolation point.
        cubx : numpy.ndarray, shape (mnlub,), optional
            Value of the nonlinear inequality constraint function at ``x``. It
            is required only if `x` is not an integer, and is not considered if
            `x` represents an interpolation point.
        ceqx : numpy.ndarray, shape (mnleq,), optional
            Value of the nonlinear equality constraint function at ``x``. It is
            required only if `x` is not an integer, and is not considered if `x`
            represents an interpolation point.

        Returns
        -------
        float
            Residual associated with the constraints of the nonlinear
            optimization problem at `x`.
        """
        if isinstance(x, (int, np.integer)):
            cubx = self.cvalub[x, :]
            ceqx = self.cvaleq[x, :]
            x = self.xpt[x, :]
        cub = np.r_[np.dot(self.aub, x) - self.bub, cubx]
        ceq = np.r_[np.dot(self.aeq, x) - self.beq, ceqx]
        cbd = np.r_[x - self.xu, self.xl - x]
        return np.max(np.r_[cub, np.abs(ceq), cbd], initial=0.)

    def check_models(self, stack_level=2):
        """
        Check whether the evaluations of the quadratic models at the
        interpolation points match their expected values.

        Parameters
        ----------
        stack_level : int, optional
            Stack level of the warning (the default is 2).

        Warns
        -----
        RuntimeWarning
            The evaluations of a quadratic function do not satisfy the
            interpolation conditions up to a certain tolerance.
        """
        stack_level += 1
        self._obj.check_model(self.xpt, self.fval, self.kopt, stack_level)
        for i in range(self.mnlub):
            self._cub[i].check_model(self.xpt, self.cvalub[:, i], self.kopt,
                                     stack_level)
        for i in range(self.mnleq):
            self._ceq[i].check_model(self.xpt, self.cvaleq[:, i], self.kopt,
                                     stack_level)

    def _get_point_to_remove(self, beta, vlag):
        """
        Select a point to remove from the interpolation set.

        Parameters
        ----------
        beta : float
            Parameter beta involved in the denominator of the updating formula.
        vlag : numpy.ndarray, shape (2 * npt,)
            Vector whose first ``npt`` components are evaluations of the
            Lagrange polynomials associated with the interpolation points.

        Returns
        -------
        int
            Index of the point to remove from the interpolation.

        Notes
        -----
        The point to remove is chosen to maximize the product absolute value of
        the denominator in Equation (2.12) of [1]_ with the quartic power of the
        distance between the point and ``self.xopt``.

        References
        ----------
        .. [1] M. J. D. Powell. "On updating the inverse of a KKT matrix." In:
           Numerical Linear Algebra and Optimization. Ed. by Y. Yuan. Beijing,
           CN: Science Press, 2004, pp. 56--78.
        """
        npt = self.xpt.shape[0]
        zsq = self.zmat ** 2.
        zsq = np.c_[-zsq[:, :self.idz], zsq[:, self.idz:]]
        alpha = np.sum(zsq, axis=1)
        sigma = vlag[:npt] ** 2. + beta * alpha
        dsq = np.sum((self.xpt - self.xopt[np.newaxis, :]) ** 2., axis=1)
        return np.argmax(np.abs(sigma) * np.square(dsq))

    def _beta(self, step):
        """
        Evaluate the parameter beta involved in the denominator of the updating
        formula for the trial point ``self.xopt + step``.

        Parameters
        ----------
        step : numpy.ndarray, shape (n,)
            Displacement from ``self.xopt`` of the trial step included in the
            parameter beta involved in the denominator of the updating formula.

        Returns
        -------
        beta : float
            Parameter beta involved in the denominator of the updating formula.
        vlag : numpy.ndarray, shape (2 * npt,)
            Vector whose first ``npt`` components are the evaluations of the
            Lagrange polynomials associated with the interpolation points at
            ``self.xopt + step``. The remaining components of `vlag` are not
            meaningful, but are involved in several updating formulae.
        """
        npt, n = self.xpt.shape
        vlag = np.empty(npt + n, dtype=float)
        stepsq = np.inner(step, step)
        xoptsq = np.inner(self.xopt, self.xopt)
        stx = np.inner(step, self.xopt)
        xstep = np.dot(self.xpt, step)
        xxopt = np.dot(self.xpt, self.xopt)
        check = xstep * (.5 * xstep + xxopt)
        zalt = np.c_[-self.zmat[:, :self.idz], self.zmat[:, self.idz:]]
        temp = np.dot(zalt.T, check)
        beta = np.inner(temp[:self.idz], temp[:self.idz])
        beta -= np.inner(temp[self.idz:], temp[self.idz:])
        vlag[:npt] = np.dot(self.bmat[:npt, :], step)
        vlag[:npt] += np.dot(self.zmat, temp)
        vlag[self.kopt] += 1.
        vlag[npt:] = np.dot(self.bmat[:npt, :].T, check)
        bsp = np.inner(vlag[npt:], step)
        vlag[npt:] += np.dot(self.bmat[npt:, :], step)
        bsp += np.inner(vlag[npt:], step)
        beta += stx ** 2. + stepsq * (xoptsq + 2. * stx + .5 * stepsq) - bsp
        return beta, vlag


class Quadratic:
    """
    Representation of a quadratic multivariate function.

    Notes
    -----
    To improve the computational efficiency of the updates of the models, the
    Hessian matrix of a model is stored as an explicit and an implicit part,
    which define the model relatively to the coordinates of the interpolation
    points [1]_. Initially, the explicit part of the Hessian matrix is zero and
    so, is not explicitly stored.

    References
    ----------
    .. [1] M. J. D. Powell. "The NEWUOA software for unconstrained optimization
       without derivatives." In: Large-Scale Nonlinear Optimization. Ed. by G.
       Di Pillo and M. Roma. New York, NY, US: Springer, 2006, pp. 255--297.
    """

    def __init__(self, bmat, zmat, idz, fval):
        """
        Construct a quadratic function by underdetermined interpolation.

        Parameters
        ----------
        bmat : numpy.ndarray, shape (npt + n, n)
            Last ``n`` columns of the inverse KKT matrix of interpolation.
        zmat : numpy.ndarray, shape (npt, npt - n - 1)
            Rank factorization matrix of the leading ``npt`` submatrix of the
            inverse KKT matrix of interpolation.
        idz : int
            Number of nonpositive eigenvalues of the leading ``npt`` submatrix
            of the inverse KKT matrix of interpolation. Although its theoretical
            value is always 0, it is designed to tackle frenumerical difficulties
            caused by ill-conditioned problems.
        fval : int or numpy.ndarray, shape (npt,)
            Evaluations associated with the interpolation points. An integer
            value represents the ``npt``-dimensional vector whose components are
            all zero, except the `fval`-th one whose value is one. Hence,
            passing an integer value construct the `fval`-th Lagrange polynomial
            associated with the interpolation points.
        """
        npt = zmat.shape[0]
        if isinstance(fval, (int, np.integer)):
            # The gradient of the fval-th Lagrange quadratic model is the
            # product of the first npt columns of the transpose of bmat with the
            # npt-dimensional vector whose components are zero, except the
            # fval-th one whose value is one.
            self._gq = np.copy(bmat[fval, :])
        else:
            self._gq = np.dot(bmat[:npt, :].T, fval)
        self._pq = omega_product(zmat, idz, fval)

        # Initially, the explicit part of the Hessian matrix of the model is the
        # zero matrix. To improve the computational efficiency of the code, it
        # is not explicitly initialized, and is stored only when updating the
        # model, if it might become a nonzero matrix.
        self._hq = None

    def __call__(self, x, xpt, kopt):
        """
        Evaluate the quadratic function.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.
        kopt : int
            Index of the interpolation point around which the quadratic function
            is defined. Since the constant term of the quadratic function is not
            maintained, ``self.__call__(xpt[kopt, :], xpt, kopt)`` must be zero.

        Returns
        -------
        float
            Value of the quadratic function at `x`.
        """
        x = x - xpt[kopt, :]
        qx = np.inner(self.gq, x)
        qx += .5 * np.inner(self.pq, np.dot(xpt, x) ** 2.)
        if self._hq is not None:
            # If the explicit part of the Hessian matrix is not defined, it is
            # understood as the zero matrix. Therefore, if self.hq is None, the
            # second-order term is entirely defined by the implicit part of the
            # Hessian matrix of the quadratic function.
            qx += .5 * np.inner(x, np.dot(self.hq, x))
        return qx

    @property
    def gq(self):
        """
        Get the stored gradient of the model.

        Returns
        -------
        numpy.ndarray, shape (n,)
            Stored gradient of the model.
        """
        return self._gq

    @property
    def pq(self):
        """
        Get the stored implicit part of the Hessian matrix of the model.

        Returns
        -------
        numpy.ndarray, shape (npt,)
            Stored implicit part of the Hessian matrix of the model.
        """
        return self._pq

    @property
    def hq(self):
        """
        Get the stored explicit part of the Hessian matrix of the model.

        Returns
        -------
        numpy.ndarray, shape (n, n)
            Stored explicit part of the Hessian matrix of the model.
        """
        if self._hq is None:
            return np.zeros((self._gq.size, self._gq.size), dtype=float)
        return self._hq

    def grad(self, x, xpt, kopt):
        """
        Evaluate the gradient of the quadratic function.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the quadratic function is to be evaluated.
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.
        kopt : int
            Index of the interpolation point around which the quadratic function
            is defined. Since the constant term of the quadratic function is not
            maintained, ``self.__call__(xpt[kopt, :], xpt, kopt)`` must be zero.

        Returns
        -------
        numpy.ndarray, shape (n,)
            Value of the gradient of the quadratic function at `x`.
        """
        return self.gq + self.hessp(x - xpt[kopt, :], xpt)

    def hess(self, xpt):
        """
        Evaluate the Hessian matrix of the quadratic function.

        Parameters
        ----------
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.

        Returns
        -------
        numpy.ndarray, shape (n, n)
            Hessian matrix of the quadratic function.

        Notes
        -----
        The Hessian matrix of the model is not explicitly stored and its
        computation requires a matrix multiplication. If only products of the
        Hessian matrix of the model with any vector are required, consider using
        instead the method ``self.hessp``.
        """
        return self.hq + np.matmul(xpt.T, self.pq[:, np.newaxis] * xpt)

    def hessp(self, x, xpt):
        """
        Evaluate the product of the Hessian matrix of the quadratic function
        with any vector.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Vector to be left-multiplied by the Hessian matrix of the quadratic
            function.
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.

        Returns
        -------
        numpy.ndarray, shape (n,)
            Value of the product of the Hessian matrix of the quadratic function
            with the vector `x`.

        Notes
        -----
        Although it is defined as ``numpy.dot(self.hess(xpt), x)``, the
        evaluation of this method improves the computational efficiency.
        """
        hx = np.dot(xpt.T, self.pq * np.dot(xpt, x))
        if self._hq is not None:
            # If the explicit part of the Hessian matrix is not defined, it is
            # understood as the zero matrix. Therefore, if self.hq is None, the
            # Hessian matrix is entirely defined by its implicit part.
            hx += np.dot(self.hq, x)
        return hx

    def curv(self, x, xpt):
        """
        Evaluate the curvature of the quadratic function.

        Parameters
        ----------
        x : numpy.ndarray, shape (n,)
            Point at which the curvature of the quadratic function is to be
            evaluated.
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.

        Returns
        -------
        float
            Curvature of the quadratic function at `x`.

        Notes
        -----
        Although it is defined as ``numpy.dot(x, self.hessp(x, xpt))``, the
        evaluation of this method improves the computational efficiency.
        """
        cx = np.inner(self.pq, np.dot(xpt, x) ** 2.)
        if self._hq is not None:
            cx += np.inner(x, np.dot(self.hq, x))
        return cx

    def shift_expansion_point(self, step, xpt):
        """
        Shift the point around which the quadratic function is defined.

        This method must be called when the index around which the quadratic
        function is defined is modified, or when the point in `xpt` around
        which the quadratic function is defined is modified.

        Parameters
        ----------
        step : numpy.ndarray, shape (n,)
            Displacement from the current point ``xopt`` around which the
            quadratic function is defined. After calling this method, the value
            of the quadratic function at ``xopt + step`` is zero, since the
            constant term of the function is not maintained.
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.
        """
        self._gq += self.hessp(step, xpt)

    def shift_interpolation_points(self, xpt, kopt):
        """
        Update the components of the quadratic function when the origin from
        which the interpolation points are defined is to be displaced.

        Parameters
        ----------
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.
        kopt : int
            Index of the interpolation point around which the quadratic function
            is defined. Since the constant term of the quadratic function is not
            maintained, ``self.__call__(xpt[kopt, :], xpt, kopt)`` must be zero.

        Notes
        -----
        Given ``xbase`` the previous origin of the calculations, it is assumed
        that it is shifted to ``xbase + xpt[kopt, :]``.
        """
        hxpt = xpt - .5 * xpt[np.newaxis, kopt, :]
        temp = np.outer(np.dot(hxpt.T, self.pq), xpt[kopt, :])
        self._hq = self.hq + temp + temp.T

    def update(self, xpt, kopt, xold, bmat, zmat, idz, knew, diff):
        """
        Update the model when a point of the interpolation set is modified.

        Parameters
        ----------
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.
        kopt : int
            Index of the interpolation point around which the quadratic function
            is defined. Since the constant term of the quadratic function is not
            maintained, ``self.__call__(xpt[kopt, :], xpt, kopt)`` must be zero.
        xold : numpy.ndarray, shape (n,)
            Previous point around which the quadratic function was defined.
        bmat : numpy.ndarray, shape (npt + n, n)
            Last ``n`` columns of the inverse KKT matrix of interpolation.
        zmat : numpy.ndarray, shape (npt, npt - n - 1)
            Rank factorization matrix of the leading ``npt`` submatrix of the
            inverse KKT matrix of interpolation.
        idz : int
            Number of nonpositive eigenvalues of the leading ``npt`` submatrix
            of the inverse KKT matrix of interpolation. Although its theoretical
            value is always 0, it is designed to tackle numerical difficulties
            caused by ill-conditioned problems.
        knew : int
            Index of the interpolation point that is modified.
        diff : float
            Difference between the evaluation of the previous model and the
            expected value at ``xpt[kopt, :]``.
        """
        # Update the explicit and implicit parts of the Hessian matrix of the
        # quadratic function. The knew-th component of the implicit part of the
        # Hessian matrix is added to the explicit Hessian matrix. Then, the
        # implicit part of the Hessian matrix is modified.
        omega = omega_product(zmat, idz, knew)
        self._hq = self.hq + self.pq[knew] * np.outer(xold, xold)
        self._pq[knew] = 0.
        self._pq += diff * omega

        # Update the gradient of the model.
        temp = omega * np.dot(xpt, xpt[kopt, :])
        self._gq += diff * (bmat[knew, :] + np.dot(xpt.T, temp))

    def check_model(self, xpt, fval, kopt, stack_level=2):
        """
        Check whether the evaluations of the quadratic function at the
        interpolation points match their expected values.

        Parameters
        ----------
        xpt : numpy.ndarray, shape (npt, n)
            Interpolation points that define the quadratic function. Each row of
            `xpt` stores the coordinates of an interpolation point.
        fval : numpy.ndarray, shape (npt,)
            Evaluations associated with the interpolation points.
        kopt : int
            Index of the interpolation point around which the quadratic function
            is defined. Since the constant term of the quadratic function is not
            maintained, ``self.__call__(xpt[kopt, :], xpt, kopt)`` must be zero.
        stack_level : int, optional
            Stack level of the warning (the default is 2).

        Warns
        -----
        RuntimeWarning
            The evaluations of the quadratic function do not satisfy the
            interpolation conditions up to a certain tolerance.
        """
        npt = fval.size
        eps = np.finfo(float).eps
        tol = 10. * np.sqrt(eps) * npt * np.max(np.abs(fval), initial=1.)
        diff = 0.
        for k in range(npt):
            qx = self(xpt[k, :], xpt, kopt)
            diff = max(diff, abs(qx + fval[kopt] - fval[k]))
        if diff > tol:
            stack_level += 1
            message = f'error in interpolation conditions is {diff:e}.'
            warnings.warn(message, RuntimeWarning, stacklevel=stack_level)
