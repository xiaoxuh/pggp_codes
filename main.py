import time
from src.nesymres.architectures.model import Model
from data_conversion import *
import numpy as np
import argparse
import operator
import math
import os
import random
from functools import partial
from deap import algorithms
from deap import base
from deap import creator
from deap import tools
from deap import gp
import torch
import json, re
import omegaconf
import sympy
from src.nesymres.dclasses import FitParams, BFGSParams
from backpropagation import *
import warnings

warnings.filterwarnings("ignore" )



def transformer_init():
    with open('jupyter/100M/eq_setting.json', 'r') as json_file:
        eq_setting = json.load(json_file)

    cfg = omegaconf.OmegaConf.load("100M/config.yaml")
    return eq_setting, cfg


def get_res_transformer(X, y, BFGS, first_call=False):
    input_X = np.array(X)
    input_Y = np.array(y)
    X = torch.from_numpy(input_X)
    y = torch.from_numpy(input_Y)

    eq_setting, cfg = transformer_init()

    bfgs = BFGSParams(
        activated=cfg.inference.bfgs.activated,
        n_restarts=cfg.inference.bfgs.n_restarts,
        add_coefficients_if_not_existing=cfg.inference.bfgs.add_coefficients_if_not_existing,
        normalization_o=cfg.inference.bfgs.normalization_o,
        idx_remove=cfg.inference.bfgs.idx_remove,
        normalization_type=cfg.inference.bfgs.normalization_type,
        stop_time=cfg.inference.bfgs.stop_time,
    )

    params_fit = FitParams(word2id=eq_setting["word2id"],
                           id2word={int(k): v for k, v in eq_setting["id2word"].items()},
                           una_ops=eq_setting["una_ops"],
                           bin_ops=eq_setting["bin_ops"],
                           total_variables=list(eq_setting["total_variables"]),
                           total_coefficients=list(eq_setting["total_coefficients"]),
                           rewrite_functions=list(eq_setting["rewrite_functions"]),
                           bfgs=bfgs,
                           beam_size=cfg.inference.beam_size
                           # This parameter is a tradeoff between accuracy and fitting time
                           )
    weights_path = "weights/100M.ckpt"
    model = Model.load_from_checkpoint(weights_path, cfg=cfg.architecture)
    model.eval()
    if torch.cuda.is_available():
        model.cuda()
    fitfunc = partial(model.fitfunc, cfg_params=params_fit)

    if first_call:

        _ = fitfunc(X, y, BFGS=True)

        final_equation = model.get_equation()

        prefix_symbol_list = fitfunc(X, y, BFGS=False)

        return prefix_symbol_list, final_equation


    if BFGS:
        try:
            prefix_symbol_list = fitfunc(X, y, BFGS)
        except ValueError:
            return [None, None]

        final_equation = model.get_equation()

        return prefix_symbol_list, final_equation, model.total_c, model.total_bfgs_time
    else:
        prefix_symbol_list = fitfunc(X, y, BFGS)
        return prefix_symbol_list



def protectedMul(left, right):
    try:
        return left*right
    except OverflowError:
        return 1e7

def protectedDiv(left, right):
    if right == 0:
        return left
    res = left / right
    if res > 1e7:
        return 1e7
    if res < -1e7:
        return -1e7
    return res
def protectedExp(arg):
    if is_complex(arg):
        return 99999
    if arg > 10:
        arg = 10
    return math.exp(arg)

def protectedLog(arg):
    if abs(arg) < 1e-5:
        arg = 1e-5
    return math.log(abs(arg))


def protectedAsin(x):
    if x < -1.0 or x > 1.0:
        return 99999
    else:
        return math.asin(x)

def protectedSqrt(x):
    if x < 0:
        return 99999
    else:
        return math.sqrt(x)

def protectedAcos(x):
    if x < -1.0 or x > 1.0:
        return 99999
    else:
        return math.asin(x)

def protectedAtan(x):
    try:
        # Calculate atan
        return math.atan(x)
    except Exception as e:
        # Handle exceptions (e.g., invalid input)
        print(f"Error: {e}")
        # Return a default value or handle the error as needed
        return None


def convert_to_list(trimmed_eq, accurate_constant=False):

    token_list= []
    for i in range(n_variables):
        token_list.append('x_'+str(i+1))

    for i, x in enumerate(trimmed_eq):
        if x == 'constant' :
            if not accurate_constant:
                trimmed_eq[i]='rand505'

        elif  x.startswith('x_'):
            index=int(x[-1])
            trimmed_eq[i]='x_'+str(index-1)
        else:

            trimmed_eq[i] = x

    return trimmed_eq



def get_creator():
    pset = gp.PrimitiveSet("MAIN", n_variables)
    rename_kwargs = {"ARG{}".format(i): 'x_'+str(i) for i in range(n_variables)}
    for k, v in rename_kwargs.items():
        pset.mapping[k].name = v
    pset.renameArguments(**rename_kwargs)
    pset.addPrimitive(operator.add, 2)
    pset.addPrimitive(operator.sub, 2)
    pset.addPrimitive(protectedMul, 2, name='mul')
    pset.addPrimitive(protectedDiv, 2, name='div')
    pset.addPrimitive(protectedExp, 1, name="exp")
    pset.addPrimitive(protectedLog, 1, name="ln")
    pset.addPrimitive(protectedSqrt, 1, name="sqrt")
    pset.addPrimitive(operator.pow, 2, name="pow")
    pset.addPrimitive(operator.abs, 1, name="abs")
    pset.addPrimitive(math.sin, 1)
    pset.addPrimitive(math.cos, 1)
    pset.addPrimitive(math.tan, 1)
    pset.addPrimitive(protectedAsin, 1,name='asin')
    pset.addPrimitive(protectedAcos, 1,name='acos')
    pset.addPrimitive(protectedAtan, 1, name='atan')
    pset.addEphemeralConstant("rand505", partial(random.uniform, -5, 5))

    creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
    creator.create("Individual", gp.PrimitiveTree, fitness=creator.FitnessMin)

    return pset, creator

def init_individual(pset, creator, trimmed_eq):

    plist=[]

    for t in trimmed_eq:
        if t in pset.mapping:
            plist.append(pset.mapping[t])

        elif t in [ '-3','-2','-1','0', '1', '2', '3', '4', '5']:

            if t not in pset.terminals[pset.ret]:
                pset.addTerminal(float(t), name=t)
                term = pset.terminals[pset.ret][-1]

            else:
                for i, term in enumerate(pset.terminals[pset.ret]):
                    if term.name == t:
                        break
                term = pset.terminals[pset.ret][i]()

            plist.append(term)


        elif t=='rand505':

            index_rand505=n_variables
            term = pset.terminals[pset.ret][index_rand505]()

            plist.append(term)

        else:
            value = float(t)

            pset.addTerminal(value, name=t)

            plist.append(pset.terminals[pset.ret][-1])

    individual = creator.Individual(gp.PrimitiveTree(plist))

    return individual

def is_complex(number):
    """
    Determine whether the given number is a complex number.

    :param number: The number to be checked.
    :return: True if the number is complex, False otherwise.
    """
    # A number is complex if it has a non-zero imaginary part
    return isinstance(number, complex) and number.imag != 0


def evalSymbReg(individual, pset, toolbox):

    func = toolbox.compile(expr=individual)
    sqerrors=[]
    wrong_mark=999999999
    for i, x in enumerate(input_X):
        try:
            try:
                try:
                    result=func(*x)
                except AttributeError:
                    print('AttributeError: NoneType object has no attribute __import__')
                    return wrong_mark,
                except ValueError:
                    print('ValueError: math domain error')
                    return wrong_mark,
                except ZeroDivisionError:
                    print('ZeroDivisionError: float division by zero')
                    return wrong_mark,

                if is_complex(result):
                    return wrong_mark,
                else:
                    tmp = (func(*x) - input_Y[i]) ** 2
                    sqerrors.append(tmp)
            except TypeError:
                print('TypeError: cannot unpack non-iterable float object')

                return wrong_mark,

        except OverflowError:
            return wrong_mark,
    try:
        res = math.sqrt(math.fsum(sqerrors) / len(input_X))
    except TypeError:
        print("TypeError: cannot convert complex to float")
        return  wrong_mark,

    return res,

def mutReplace(individual, pset, toolbox, creator):

    global  input_X
    node_index = random.randrange(len(individual))
    if len(individual) == 1:
        return gp.mutUniform(individual, expr=toolbox.expr_mut, pset=pset)
    while individual[node_index].arity == 0:
        node_index = random.randrange(len(individual))
    try:
        semantic = backpropogation(individual, pset, (input_X, input_Y), node_index)
    except OverflowError:
        return gp.mutUniform(individual, expr=toolbox.expr_mut, pset=pset)
    if semantic == 'nan':
        return gp.mutUniform(individual, expr=toolbox.expr_mut, pset=pset)

    try:
        symbol_list, prediceted_equation,total_c, bfgs_time= get_res_transformer(input_X, semantic, BFGS=True)

    except ValueError:
        return gp.mutUniform(individual, expr=toolbox.expr_mut, pset=pset)

    if symbol_list is None:
        return gp.mutUniform(individual, expr=toolbox.expr_mut, pset=pset)



    symbol_list=convert_to_list(symbol_list,accurate_constant=True)


    new_subtree=init_individual(pset, creator, symbol_list)

    CT_slice = individual.searchSubtree(node_index)

    individual[CT_slice] = new_subtree

    return individual,

def mutate(individual, pset, creator, toolbox, p_subtree=0.05):

    if random.random() < p_subtree:

        return mutReplace(individual, pset=pset, toolbox=toolbox, creator=creator)
    else:

        return gp.mutUniform(individual, expr=toolbox.expr_mut, pset=pset)


def main(filename):

    dataset_path = "../benchmark_dataset/"

    file_path = os.path.join(dataset_path, filename + '.txt')
    global n_variables
    global input_X
    global input_Y
    file_contents = {}

    input_X = []
    input_Y = []

    if os.path.isfile(file_path):
        try:
            with open(file_path, 'r') as file:

                for line in file:

                    data = line.strip().split()

                    x = list(map(float, data[:-1]))
                    y = float(data[-1])
                    input_X.append(x)
                    input_Y.append(y)

        except Exception as e:
            file_contents[filename] = f"Error reading file: {e}"

    n_variables= len(input_X[0])

    test_dataset_path = "../benchmark_test/"
    file_path = os.path.join(test_dataset_path, filename + '.txt')

    test_X = []
    test_Y = []

    if os.path.isfile(file_path):
        try:
            with open(file_path, 'r') as file:

                for line in file:
                    data = line.strip().split()

                    x = list(map(float, data[:-1]))
                    y = float(data[-1])
                    test_X.append(x)
                    test_Y.append(y)

        except Exception as e:
            file_contents[filename] = f"Error reading file: {e}"

    test_X = np.array(test_X)
    test_Y = np.array(test_Y)


    pset, creator = get_creator()

    # fixme

    symbol_list, equation, =  get_res_transformer(input_X, input_Y, BFGS=False, first_call=True)

    total_variables = ["x_1", "x_2", "x_3"][:n_variables]

    X_dict = {x: test_X[:, idx] for idx, x in enumerate(total_variables)}
    y_pred = np.array(sympy.lambdify(",".join(total_variables), equation)(**X_dict))

    transformer_rmse = root_mean_squared_error(test_Y.ravel(), y_pred.ravel())

    # early stop
    if transformer_rmse < 1e-10:
        print('early stop')
        return

    # fixme
    trimmed_eq = convert_to_list(symbol_list, accurate_constant=False)


    # set toolbox
    toolbox = base.Toolbox()
    toolbox.register("expr", gp.genHalfAndHalf, pset=pset, min_=2, max_=6)
    toolbox.register("random_individual", tools.initIterate, creator.Individual, toolbox.expr)
    toolbox.register("individual", init_individual, pset, creator, trimmed_eq=trimmed_eq)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("random_population", tools.initRepeat, list, toolbox.random_individual)
    toolbox.register("expr_mut", gp.genFull, min_=0, max_=2)
    # toolbox.register("population", init_population, list, creator.Individual, eq=trimmed_eq, primitive_set=pset, num_individuals=20)
    toolbox.register("compile", gp.compile, pset=pset)
    toolbox.register("evaluate", evalSymbReg, pset=pset, toolbox=toolbox)
    toolbox.register("select", tools.selTournament, tournsize=3)
    toolbox.register("mate", gp.cxOnePoint)
    toolbox.register("mutate", mutate, pset=pset, creator=creator, toolbox=toolbox, p_subtree=0.025)
    toolbox.decorate("mate", gp.staticLimit(key=operator.attrgetter("height"), max_value=17))
    toolbox.decorate("mutate", gp.staticLimit(key=operator.attrgetter("height"), max_value=17))

    np.random.seed(8346)
    random.seed(8346)

    pop = []
    transformer_init=toolbox.population(n=1)
    for i in range(20):
        pop+=copy.deepcopy(transformer_init)

    pop+=toolbox.random_population(n=200-len(pop))


    hof = tools.HallOfFame(1)
    stats_fit = tools.Statistics(lambda ind: ind.fitness.values)
    stats_size = tools.Statistics(len)
    mstats = tools.MultiStatistics(fitness=stats_fit, size=stats_size)
    mstats.register("avg", np.mean)
    mstats.register("std", np.std)
    mstats.register("min", np.min)
    mstats.register("max", np.max)


    start_time = time.time()
    pop, log, rmse_mid, test_rmse_mid, generations = algorithms.eaSimple(n_variables,pop, pset, toolbox, test_X,test_Y,0.5, 0.2, ngen=300, stats=mstats,
                                   halloffame=hof, verbose=True)
    end_time = time.time()

    training_time=end_time - start_time

    last_generation_fitness = np.min([ind.fitness.values[0] for ind in pop])


    func = toolbox.compile(expr=hof[0])



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Transformer-GP')
    parser.add_argument('dataset_name', type=str, default='Constant-1')
    args = parser.parse_args()
    main(args.dataset_name)
