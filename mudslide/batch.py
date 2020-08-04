# -*- coding: utf-8 -*-
"""Code for running batches of trajectories"""

from __future__ import print_function, division
import queue
import sys

import numpy as np

from .exceptions import StillInteracting
from .tracer import TraceManager

from typing import Any, Iterator, Tuple
from .typing import ModelT, TrajGenT, ArrayLike, DtypeLike

#####################################################################################
# Canned classes act as generator functions for initial conditions                  #
#####################################################################################

class TrajGenConst(object):
    """Canned class whose call function acts as a generator for static initial conditions

    :param position: initial position
    :param momentum: initial momentum
    :param initial_state: initial state specification should be either an integer or "ground"
    :param seed: entropy seed for random generator (unused)
    """
    def __init__(self, position: ArrayLike, momentum: ArrayLike, initial_state: Any, seed: Any = None):
        self.position = position
        self.momentum = momentum
        self.initial_state = initial_state
        self.seed_sequence = np.random.SeedSequence(seed)

    def __call__(self, nsamples: int) -> Iterator:
        """Generate nsamples initial conditions

        :param nsamples: number of initial conditions to generate
        """
        seedseqs = self.seed_sequence.spawn(nsamples)
        for i in range(nsamples):
            yield (self.position, self.momentum, self.initial_state, { "seed_sequence" : seedseqs[i] })

class TrajGenNormal(object):
    """Canned class whose call function acts as a generator for normally distributed initial conditions"""
    def __init__(self, position: ArrayLike, momentum: ArrayLike, initial_state: Any, sigma: ArrayLike, seed: Any = None, seed_traj: Any = None):
        """
        :param position: center of normal distribution for position
        :param momentum: center of normal distribution for momentum
        :param initial_state: initial state designation
        :param sigma: standard deviation of distribution
        :param seed: initial seed to give to trajectory
        """
        self.position = position
        self.position_deviation = 0.5 * sigma
        self.momentum = momentum
        self.momentum_deviation = 1.0 / sigma
        self.initial_state = initial_state
        self.seed_sequence = np.random.SeedSequence(seed)
        self.random_state = np.random.np.random.default_rng(seed_traj)

    def kskip(self, ktest: float) -> bool:
        """Whether to skip given momentum
        :param ktest: momentum

        :returns: True/False
        """
        return ktest < 0.0

    def __call__(self, nsamples: int) -> Iterator:
        """Generate nsamples initial conditions
        :param nsamples: number of initial conditions requested
        """
        seedseqs = self.seed_sequence.spawn(nsamples)
        for i in range(nsamples):
            x = self.random_state.normal(self.position, self.position_deviation)
            k = self.random_state.normal(self.momentum, self.momentum_deviation)

            if (self.kskip(k)): continue
            yield (x, k, self.initial_state, { "seed_sequence" : seedseqs[i] })


class BatchedTraj(object):
    """Class to manage many TrajectorySH trajectories

    Requires a model object which is a class that has functions V(x), dV(x), nstates(), and ndim()
    that return the Hamiltonian at position x, gradient of the Hamiltonian at position x
    number of electronic states, and dimension of nuclear space, respectively.
    """
    def __init__(self, model: ModelT, traj_gen: TrajGenT, trajectory_type: Any, tracemanager: Any = None, **inp: Any):
        """Constructor requires model and options input as kwargs
        :param model: object used to describe the model system
        :param traj_gen: generator object to generate initial conditions
        :param trajectory_type: surface hopping trajectory class
        :param tracemanager: object to collect results
        :param inp: input options

         Accepted keyword arguments and their defaults:
         | key                |   default                  |
         ---------------------|----------------------------|
         | initial_time       | 0.0                        |
         | samples            | 2000                       |
         | dt                 | 20.0  ~ 0.5 fs             |
         | nprocs             | MultiProcessing.cpu_count  |
         | outcome_type       | "state"                    |
         | seed               | None (date)                |
        """
        self.model = model
        if tracemanager is None:
            self.tracemanager = TraceManager()
        else:
            self.tracemanager = tracemanager
        self.trajectory = trajectory_type
        self.traj_gen = traj_gen
        self.options = {}

        # time parameters
        self.options["initial_time"]  = inp.get("initial_time", 0.0)

        # statistical parameters
        self.options["samples"]       = inp.get("samples", 2000)
        self.options["dt"]            = inp.get("dt", 20.0) # default to roughly half a femtosecond

        self.options["nprocs"]        = inp.get("nprocs", 1)
        self.options["outcome_type"]  = inp.get("outcome_type", "state")

        # everything else just gets copied over
        for x in inp:
            if x not in self.options:
                self.options[x] = inp[x]

    def run_trajectories(self, n: int) -> Tuple:
        """Runs a set of trajectories and collects the results

        :param n: number of trajectories to run
        """
        outcomes = np.zeros([self.model.nstates(),2], dtype=np.float64)
        traces = []
        try:
            for x0, p0, initial, params in self.traj_gen(n):
                traj_input = self.options
                traj_input.update(params)
                traj = self.trajectory(self.model, x0, p0, initial, self.tracemanager.spawn_tracer(), **traj_input)
                try:
                    trace = traj.simulate()
                    traces.append(trace)
                    outcomes += traj.outcome()
                except StillInteracting:
                    print("BEWARE: a simulation ended while still in interaction region", file=sys.stderr)
                    pass
            return (outcomes, traces)
        except KeyboardInterrupt:
            raise

    def compute(self) -> TraceManager:
        """Run batch of trajectories and return aggregate results

        :returns: TraceManager containing the results
        """
        # for now, define four possible outcomes of the simulation
        outcomes = np.zeros([self.model.nstates(),2], dtype=np.float64)
        nsamples = int(self.options["samples"])
        nprocs = self.options["nprocs"]

        traj_queue: Any = queue.Queue()
        results_queue: Any = queue.Queue()

        #traj_queue = mp.JoinableQueue()
        #results_queue = mp.Queue()
        #procs = [ mp.Process(target=traj_runner, args=(traj_queue, results_queue, )) for p in range(nprocs) ]
        #for p in procs:
        #    p.start()

        for x0, p0, initial, params in self.traj_gen(nsamples):
            traj_input = self.options
            traj_input.update(params)
            traj = self.trajectory(self.model, x0, p0, initial, self.tracemanager.spawn_tracer(),
                    queue=traj_queue, **traj_input)
            traj_queue.put(traj)

        while not traj_queue.empty():
            traj = traj_queue.get()
            results = traj.simulate()
            results_queue.put(results)

        #traj_queue.join()
        #for p in procs:
        #    p.terminate()

        while not results_queue.empty():
            r = results_queue.get()
            self.tracemanager.merge_tracer(r)

        self.tracemanager.outcomes = self.tracemanager.outcome()
        return self.tracemanager

def traj_runner(traj_queue: Any, results_queue: Any) -> None:
    """Runner for computing jobs from queue

    :param traj_queue: queue containing trajectories with a `simulate()` function
    :param results_queue: queue to store results of each call to `simulate()`
    """
    while True:
        traj = traj_queue.get()
        if traj is not None:
            results = traj.simulate()
            results_queue.put(results)
        traj_queue.task_done()

def unwrapped_run_trajectories(fssh: Any, n: int) -> Tuple:
    """global version of BatchedTraj.run_trajectories that
    is necessary because of the stupid way threading pools work in python

    :param fssh: BatchedTraj object
    :param n: number of trajectories to run
    """
    try:
        return BatchedTraj.run_trajectories(fssh, n)
    except KeyboardInterrupt:
        raise