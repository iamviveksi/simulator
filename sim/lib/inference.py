import time
import os
import sys
import asyncio
import threading
import json
import pprint
import csv
from datetime import datetime

from lib.priorityqueue import PriorityQueue
from lib.dynamics import DiseaseModel
from lib.mobilitysim import MobilitySimulator
from lib.parallel import *

import gpytorch, torch, botorch, sobol_seq
from botorch import fit_gpytorch_model
from botorch.models.transforms import Standardize
from botorch.models import FixedNoiseGP, ModelListGP, HeteroskedasticSingleTaskGP
from gpytorch.mlls.sum_marginal_log_likelihood import SumMarginalLogLikelihood
from gpytorch.mlls import ExactMarginalLogLikelihood, MarginalLogLikelihood
from botorch.acquisition.monte_carlo import MCAcquisitionFunction, qNoisyExpectedImprovement, qSimpleRegret
from botorch.acquisition.objective import MCAcquisitionObjective
from botorch.acquisition.max_value_entropy_search import qMaxValueEntropy
from botorch.acquisition import OneShotAcquisitionFunction
import botorch.utils.transforms as transforms
from botorch.utils.transforms import match_batch_shape, t_batch_mode_transform

from botorch.sampling.samplers import SobolQMCNormalSampler, IIDNormalSampler
from botorch.exceptions import BadInitialCandidatesWarning
from botorch.optim import optimize_acqf
from botorch.acquisition.objective import GenericMCObjective, ConstrainedMCObjective
from botorch.gen import get_best_candidates, gen_candidates_torch
from botorch.optim import gen_batch_initial_conditions

from lib.inference_kg import qKnowledgeGradient, gen_one_shot_kg_initial_conditions
from lib.distributions import CovidDistributions
from lib.settings.calibration_settings import settings_model_param_bounds, settings_testing_params
from lib.data import collect_data_from_df

from lib.measures import (
    MeasureList,
    SocialDistancingForAllMeasure,
    SocialDistancingByAgeMeasure,
    SocialDistancingForPositiveMeasure,
    SocialDistancingForPositiveMeasureHousehold,
    Interval)


import warnings
warnings.filterwarnings('ignore', category=BadInitialCandidatesWarning)
warnings.filterwarnings('ignore', category=RuntimeWarning)
warnings.filterwarnings('ignore', category=UserWarning)

MIN_NOISE = torch.tensor(1e-6)


def save_state(obj, filename):
    """Saves `obj` to filename"""
    with open('logs/' + filename + '_state.pk', 'wb') as fp:
        torch.save(obj, fp)
    return

def load_state(filename):
    with open(filename, 'rb') as fp:
        obj = torch.load(fp)
    return obj

class CalibrationLogger:

    def __init__(
        self,
        filename,
        verbose
    ):

        self.dir = 'logs/'
        self.filename = filename
        self.headers = [
            'iter',
            '    best obj',
            ' current obj',
            ' diff',
            'b/educat',
            'b/social',
            'b/bus_st',
            'b/office',
            'b/superm',
            'b/househ',
            '      mu',
            'walltime',
        ]

        self.verbose = verbose

    def log_initial_lines(self, initial_lines):
        '''
        Writes `initial_lines` to top of log file.
        '''

        self.initial_lines = initial_lines

        # write headers
        with open(f'{self.dir + self.filename}.csv', 'w+') as logfile:

            wr = csv.writer(logfile, quoting=csv.QUOTE_ALL)
            for l in self.initial_lines:
                wr.writerow([l])
            wr.writerow([""])
            wr.writerow(self.headers)

        # print to stdout if verbose
        if self.verbose:
            for l in self.initial_lines:
                print(l)
            print()
            headerstrg = ' | '.join(self.headers)
            print(headerstrg)

    def log(self, i, time, best, objective, case_diff, theta):

        '''
        Writes lst to a .csv file
        '''
        d = parr_to_pdict(theta)
        fields = [
            f"{i:4.0f}",
            f"{best:12.4f}",
            f"{objective:12.4f}",
            f"{case_diff:5.0f}",
            f"{d['betas']['education']:8.4f}",
            f"{d['betas']['social']:8.4f}",
            f"{d['betas']['bus_stop']:8.4f}",
            f"{d['betas']['office']:8.4f}",
            f"{d['betas']['supermarket']:8.4f}",
            f"{d['beta_household']:8.4f}",
            f"{d['mu']:8.4f}",
            f"{time/60.0:8.4f}",
        ]

        with open(f'{self.dir + self.filename}.csv', 'a') as logfile:

            wr = csv.writer(logfile, quoting=csv.QUOTE_ALL)
            wr.writerow(fields)

        # print to stdout if verbose
        if self.verbose:
            outstrg = ' | '.join(list(map(str, fields)))
            print(outstrg)

        return

def pdict_to_parr(d):
    """Convert parameter dict to BO parameter tensor"""
    arr = torch.stack([
        torch.tensor(d['betas']['education']),
        torch.tensor(d['betas']['social']),
        torch.tensor(d['betas']['bus_stop']),
        torch.tensor(d['betas']['office']),
        torch.tensor(d['betas']['supermarket']),
        torch.tensor(d['beta_household']),
        torch.tensor(d['mu']),
    ])
    return arr

def parr_to_pdict(arr):
    """Convert BO parameter tensor to parameter dict"""
    d = {
        'betas': {
            'education': arr[0].tolist(),
            'social': arr[1].tolist(),
            'bus_stop': arr[2].tolist(),
            'office': arr[3].tolist(),
            'supermarket': arr[4].tolist(),
        },
        'beta_household': arr[5].tolist(),
        'mu': arr[6].tolist()
    }
    return d

def gen_initial_seeds(cases):
    """
    Generates initial seed counts based on `cases` np.array.
    `cases` has to have shape (num_days, num_age_groups).

    Assumptions:
    - Cases on day t=0 set to number of symptomatic `isym` and positively tested
    - Following literature, asyptomatic indiviudals `iasy` make out approx `alpha` percent of all symtomatics
    - Following literature on R0, set `expo` = R0 * (`isym` + `iasy`)
    - Recovered cases are also considered
    - All other seeds are omitted
    
    """

    num_days, num_age_groups = cases.shape

    # set initial seed count (approximately based on infection counts on March 10)
    dists = CovidDistributions(fatality_rates_by_age=np.zeros(num_age_groups))
    alpha = dists.alpha
    isym = cases[0].sum().item()
    iasy = alpha / (1 - alpha) * isym
    expo = dists.R0 * (isym + iasy)

    seed_counts = {
        'expo': math.ceil(expo),
        'isym_posi': math.ceil(isym),
        'iasy': math.ceil(iasy),
    }
    return seed_counts

def convert_timings_to_cumulative_daily(timings, age_groups, time_horizon):
    '''

    Converts batch of size N of timings of M individuals of M age indicators `age_groups` in a time horizon 
    of `time_horizon` in hours into daily cumulative aggregate cases 

    Argument:
        timings :   np.array of shape (N, M)
        age_groups: np.array of shape (N, M)

    Returns:
        timings :   np.array of shape (N, T / 24, `number of age groups`)
    '''
    if len(timings.shape) == 1:
        timings = np.expand_dims(timings, axis=0)

    num_age_groups = torch.unique(age_groups).shape[0]

    # cumulative: (N, T // 24, num_age_groups)
    cumulative = torch.zeros((timings.shape[0], int(time_horizon // 24), num_age_groups))
    for t in range(0, int(time_horizon // 24)):
        for a in range(num_age_groups):
            cumulative[:, t, a] = torch.sum(((timings < (t + 1) * 24) & (age_groups == a)), dim=1)

    return cumulative

def make_bayes_opt_functions(args): 
    '''
    Generates and returns functions used to run Bayesian optimization
    Argument:
        args:                   Keyword arguments specifying exact settings for optimization

    Returns:
        objective :                         objective maximized for BO
        generate_initial_observations :     function to generate initial observations
        initialize_model :                  function to initialize GP
        optimize_acqf_and_get_observation : function to optimize acquisition function based on model
        case_diff :                         computes case difference between prediction array and ground truth at t=T
        unnormalize_theta :                 converts BO params to simulation params (unit cube to real parameters)
        header :                            header lines to be printed to log file

    '''
    header = []

    # remember line executed
    header.append('=' * 100)
    header.append(datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
    header.append('python ' + ' '.join(sys.argv))
    header.append('=' * 100)

    mob_settings = args.mob
    data_area = args.area
    data_country = args.country
    data_days = args.days
    case_downsampling = args.downsample
    
    # data settings
    verbose = not args.not_verbose
    use_households = not args.no_households
    data_start_date = args.start
    debug_simulation_days = args.endsimat

    # initialize mobility object to obtain information (no trace generation yet)
    with open(mob_settings, 'rb') as fp:
        kwargs = pickle.load(fp)
    mob = MobilitySimulator(**kwargs)

    # number of tests processed every `testing_frequency` hours
    # FIXME: better name of field: `mob.daily_tests`
    unscaled_testing_capacity = args.testingcap or mob.daily_tests_per_100k
    population_unscaled = mob.num_people_unscaled

    # simulation settings
    n_init_samples = args.ninit
    n_iterations = args.niters
    simulation_roll_outs = args.rollouts
    cpu_count = args.cpu_count
    dynamic_tracing = not args.no_dynamic_tracing
    load_observations = args.load

    # parameter bounds
    param_bounds = settings_model_param_bounds
    # param_bounds = settings_measures_param_bounds

    # set testing parameters
    testing_params = settings_testing_params

    # BO acquisition function optimization (Knowledge gradient)
    acqf_opt_num_fantasies = args.acqf_opt_num_fantasies
    acqf_opt_num_restarts = args.acqf_opt_num_restarts
    acqf_opt_raw_samples = args.acqf_opt_raw_samples
    acqf_opt_batch_limit = args.acqf_opt_batch_limit
    acqf_opt_maxiter = args.acqf_opt_maxiter

    """
    Bayesian optimization pipeline
    """

    # Based on population size, approx. 300 tests/day in Area of Tübingen (~135 in city of Tübingen)
    tests_per_day = math.ceil(unscaled_testing_capacity / case_downsampling)

    # set testing parameters
    testing_params['tests_per_batch'] = tests_per_day

    test_lag_days = int(testing_params['test_reporting_lag'] / 24.0)
    assert(int(testing_params['test_reporting_lag']) % 24 == 0)

    # Import Covid19 data
    new_cases_ = collect_data_from_df(country=data_country, area=data_area, datatype='new',
                                      start_date_string=data_start_date, days=data_days)
    resistant_cases_ = collect_data_from_df(country=data_country, area=data_area, datatype='recovered',
                                            start_date_string=data_start_date, days=data_days)
    fatality_cases_ = collect_data_from_df(country=data_country, area=data_area, datatype='fatality',
                                           start_date_string=data_start_date, days=data_days)

    assert(len(new_cases_.shape) == 2 and len(resistant_cases_.shape)
           == 2 and len(fatality_cases_.shape) == 2)

    if new_cases_[0].sum() == 0:
        print('No positive cases at provided start time; cannot seed simulation.\n'
              'Consider setting a later start date for calibration using the "--start" flag.')
        exit(0)

    # Empirical fatality rate per age group from the above data.
    # RKI data defines 6 groups: **0-4y, 5-14y, 15-34y, 35-59y, 60-79y, 80+y**
    num_age_groups = fatality_cases_.shape[1]
    fatality_rates_by_age = (fatality_cases_[-1, :] /
                             (new_cases_[-1, :] + fatality_cases_[-1, :] + resistant_cases_[-1, :]))
    fatality_rates_by_age = np.nan_to_num(
        fatality_rates_by_age)  # deal with 0/0

    # Scale down cases based on number of people in simulation
    new_cases, resistant_cases, fatality_cases = (
        np.ceil(1/case_downsampling * new_cases_),
        np.ceil(1/case_downsampling * resistant_cases_),
        np.ceil(1/case_downsampling * fatality_cases_))

    # generate initial seeds based on case numbers
    initial_seeds = gen_initial_seeds(new_cases)
    header.append('Initial seed counts : ' + str(initial_seeds))

    # in debug mode, shorten time of simulation, shorten time
    if debug_simulation_days:
        new_cases = new_cases[:debug_simulation_days]

    # Maximum time fixed by real data, init mobility simulator simulation
    # maximum time to simulate, in hours
    max_time = int(new_cases.shape[0] * 24.0)
    max_time += 24.0 * test_lag_days  # longer due to test lag in simulations
    testing_params['testing_t_window'] = [0.0, max_time]
    mob.simulate(max_time=max_time, dynamic_tracing=True)

    header.append(
        'Max time T (days): ' + str(new_cases.shape[0]))
    header.append(
        'Target cases per age group at t=0:   ' + str(list(map(int, new_cases[0].tolist()))))
    header.append(
        'Target cases per age group at t=T:   ' + str(list(map(int, new_cases[-1].tolist()))))
    header.append(
        'Fatality rates:                      ' + str(fatality_rates_by_age.tolist()))

    # instantiate correct distributions
    distributions = CovidDistributions(
        fatality_rates_by_age=fatality_rates_by_age)

    # standard quarantine of positive tests staying at home in isolation
    measure_list = MeasureList([
        SocialDistancingForPositiveMeasure(
            t_window=Interval(0.0, max_time), p_stay_home=1.0),
        SocialDistancingForPositiveMeasureHousehold(
            t_window=Interval(0.0, max_time), p_isolate=1.0),
    ])

    # set Bayesian optimization target as positive cases
    n_days, n_age = new_cases.shape
    G_obs = torch.tensor(new_cases).reshape(n_days * n_age)  # flattened

    sim_bounds = pdict_to_parr(param_bounds).T
    n_params = sim_bounds.shape[1]

    header.append(f'Parameters : {n_params}')
    header.append('Parameter bounds: ' + str(parr_to_pdict(sim_bounds.T)))

    # create settings dictionary for simulations
    launch_kwargs = dict(
        mob_settings=mob_settings,
        distributions=distributions,
        random_repeats=simulation_roll_outs,
        cpu_count=cpu_count,
        initial_seeds=initial_seeds,
        testing_params=testing_params,
        measure_list=measure_list,
        max_time=max_time,
        num_people=mob.num_people,
        num_sites=mob.num_sites,
        home_loc=mob.home_loc,
        site_loc=mob.site_loc,
        dynamic_tracing=dynamic_tracing,
        verbose=False)


    '''
    Define central functions for optimization
    '''

    G_obs = torch.tensor(new_cases).reshape(1, n_days * n_age)
    
    def composite_squared_loss(G):
        '''
        Objective function
        Note: in BO, objectives are maximized
        '''
        return - (G - G_obs).pow(2).sum(dim=-1)

    # select objective
    objective = GenericMCObjective(composite_squared_loss)

    def case_diff(preds):
        '''
        Computes case difference of predictions and ground truth at t=T
        '''
        return  preds.reshape(n_days, n_age)[-1].sum() - torch.tensor(new_cases)[-1].sum()

    def unnormalize_theta(theta):
        '''
        Computes unnormalized parameters
        '''
        return transforms.unnormalize(theta, sim_bounds)

    def composite_simulation(norm_params):
        """
        Takes a set of normalized (unit cube) BO parameters
        and returns simulator output means and standard errors based on multiple
        random restarts. This corresponds to the black-box function.
        """

        # un-normalize normalized params to obtain simulation parameters
        params = transforms.unnormalize(norm_params, sim_bounds)

        # run simulation in parallel, using simulator dictionary type for parameters
        kwargs = copy.deepcopy(launch_kwargs)
        kwargs['params'] = parr_to_pdict(params)

        summary = launch_parallel_simulations(**kwargs)

        # (random_repeats, n_people)
        posi_started = torch.tensor(summary.state_started_at['posi'])
        posi_started -= test_lag_days * 24.0 # account for test lag

        # (random_repeats, n_days)
        age_groups = torch.tensor(summary.people_age)
        posi_cumulative = convert_timings_to_cumulative_daily(
            timings=posi_started, age_groups=age_groups, time_horizon=n_days * 24.0)

        if posi_cumulative.shape[0] <= 1:
            raise ValueError('Must run at least 2 random restarts per setting to get estimate of noise in observation.')

        # compute mean and standard error of means        
        G = torch.mean(posi_cumulative, dim=0)
        G_sem = torch.std(posi_cumulative, dim=0) / math.sqrt(posi_cumulative.shape[0])

        # make sure noise is not zero for non-degerateness
        G_sem = torch.max(G_sem, MIN_NOISE)

        # flatten
        G = G.reshape(1, n_days * n_age)
        G_sem = G_sem.reshape(1, n_days * n_age)

        return G, G_sem


    def generate_initial_observations(n, logger):
        """
        Takes an integer `n` and generates `n` initial observations
        from the black box function using Sobol random parameter settings
        in the unit cube. Returns parameter setting and black box function outputs
        """

        if n <= 0:
            raise ValueError(
                'qKnowledgeGradient and GP needs at least one observation to be defined properly.')

        # sobol sequence
        # new_thetas: [n, n_params]
        new_thetas = torch.tensor(
            sobol_seq.i4_sobol_generate(n_params, n), dtype=torch.float)

        # simulator observations
        # new_G, new_G_sem: [n, n_days * n_age] (flattened outputs)
        new_G = torch.zeros((n, n_days * n_age), dtype=torch.float)
        new_G_sem = torch.zeros((n, n_days * n_age), dtype=torch.float)

        for i in range(n):

            t0 = time.time()

            # get mean and standard error of mean (sem) of every simulation output
            G, G_sem = composite_simulation(new_thetas[i, :])
            new_G[i, :] = G
            new_G_sem[i, :] = G_sem

            # log
            G_objectives = objective(new_G[:i+1])
            best_idx = G_objectives.argmax()
            best = G_objectives[best_idx].item()
            current = objective(G).item()
            case_diff = (
                G.reshape(n_days, n_age)[-1].sum()
                - G_obs.reshape(n_days, n_age)[-1].sum())

            t1 = time.time()
            logger.log(
                i=i - n,
                time=t1 - t0,
                best=best,
                objective=current,
                case_diff=case_diff,
                theta=transforms.unnormalize(new_thetas[i, :].detach().squeeze(), sim_bounds)
            )

            # save state
            state = {
                'train_theta': new_thetas[:i+1],
                'train_G': new_G[:i+1],
                'train_G_sem': new_G_sem[:i+1],
                'best_observed_obj': best,
                'best_observed_idx': best_idx,
            }
            save_state(state, logger.filename + '_init')

        # compute best objective from simulations
        f = objective(new_G)
        best_f_idx = f.argmax()
        best_f = f[best_f_idx].item()

        return new_thetas, new_G, new_G_sem, best_f, best_f_idx

    def initialize_model(train_x, train_y, train_y_sem):
        """
        Defines a GP given X, Y, and noise observations (standard error of mean)
        """
        
        train_ynoise = train_y_sem.pow(2.0) # noise is in variance units
        
        # standardize outputs to zero mean, unit variance to have good hyperparameter tuning
        model = FixedNoiseGP(train_x, train_y, train_ynoise, outcome_transform=Standardize(m=n_days * n_age))

        # "Loss" for GPs - the marginal log likelihood
        mll = ExactMarginalLogLikelihood(model.likelihood, model)

        return mll, model

    # Model initialization
    # parameters used in BO are always in unit cube for optimal hyperparameter tuning of GPs
    bo_bounds = torch.stack([torch.zeros(n_params), torch.ones(n_params)])

    def optimize_acqf_and_get_observation(acq_func, args):
        """
        Optimizes the acquisition function, and returns a new candidate and a noisy observation.
        botorch defaults:  num_restarts=10, raw_samples=256, batch_limit=5, maxiter=200
        """

        batch_initial_conditions = gen_one_shot_kg_initial_conditions(
            acq_function=acq_func,
            bounds=bo_bounds,
            q=1,
            num_restarts=args.acqf_opt_num_restarts,
            raw_samples=args.acqf_opt_raw_samples,
            options={"batch_limit": args.acqf_opt_batch_limit,
                     "maxiter": args.acqf_opt_maxiter},
        )

        # optimize
        candidates, _ = optimize_acqf(
            acq_function=acq_func,
            bounds=bo_bounds,
            q=1,
            num_restarts=args.acqf_opt_num_restarts,
            raw_samples=args.acqf_opt_raw_samples,  # used for intialization heuristic
            options={"batch_limit": args.acqf_opt_batch_limit,
                     "maxiter": args.acqf_opt_maxiter},
            batch_initial_conditions=batch_initial_conditions
        )

        # proposed evaluation
        new_theta = candidates.detach()

        # observe new noisy function evaluation
        new_G, new_G_sem = composite_simulation(new_theta.squeeze())

        return new_theta, new_G, new_G_sem

    # return functions
    return (
        objective, 
        generate_initial_observations,
        initialize_model,
        optimize_acqf_and_get_observation,
        case_diff,
        unnormalize_theta,
        header,
    )


