import os
import pickle
import itertools
import multiprocessing
from functools import partial, reduce
from datetime import datetime
from time import time
from math import gcd
from typing import List, Iterator, Callable
from collections import namedtuple
import mpmath
import sympy
from sympy import lambdify
from latex import generate_latex
from mobius import GeneralizedContinuedFraction, EfficientGCF
from convergence_rate import calculate_convergence
from series_generators import SeriesGeneratorClass, CartesianProductAnGenerator, CartesianProductBnGenerator


# intermediate result - coefficients of lhs transformation, and compact polynomials for seeding an and bn series.
Match = namedtuple('Match', 'lhs_key rhs_an_poly rhs_bn_poly')
FormattedResult = namedtuple('FormattedResult', 'LHS RHS GCF')


class GlobalHashTableInstance:
    def __init__(self):
        """
        python processes don't share memory. so when using multiprocessing the hash table will be duplicated.
        to try and avoid this, we initiate a global instance of the hash table.
        hopefully this will useful when running on linux (taking advantage of Copy On Write).
        this has not been tested yet on linux.
        on windows it has no effect when multiprocessing.
        """
        self.hash = {}
        self.name = ''


# global instance
g_hash_instance = GlobalHashTableInstance()
g_N_verify_terms = 1000
g_N_initial_search_terms = 32


class LHSHashTable(object):

    @staticmethod
    def are_co_prime(integers):
        common = integers[-1]
        for x in integers:
            common = gcd(x, common)
            if common == 1:
                return True
        return False

    @staticmethod
    def prod(coefs, consts):
        ret = coefs[0]
        for i in range(len(consts)):
            ret += consts[i] * coefs[i+1]
        return ret

    def evaluate(self, key, constant_values):
        c_top, c_bottom = self.s[key]
        numerator = self.prod(c_top, constant_values)
        denominator = self.prod(c_bottom, constant_values)
        return mpmath.mpf(numerator) / mpmath.mpf(denominator)

    def evaluate_sym(self, key, symbols):
        c_top, c_bottom = self.s[key]
        numerator = self.prod(c_top, symbols)
        denominator = self.prod(c_bottom, symbols)
        return numerator / denominator

    def __init__(self, search_range, const_vals, threshold) -> None:
        """
        hash table for LHS. storing values in the form of (a + b*x_1 + c*x_2 + ...)/(d + e*x_1 + f*x_2 + ...)
        :param search_range: range for value coefficient values
        :param const_vals: constants for x.
        :param threshold: decimal threshold for comparison. in fact, the keys for hashing will be the first
                            -log_{10}(threshold) digits of the value. for example, if threshold is 1e-10 - then the
                            first 10 digits will be used as the hash key.
        """
        self.s = {}
        self.threshold = threshold
        key_factor = 1 / threshold

        # create blacklist of rational numbers
        coef_possibilities = [i for i in range(-search_range, search_range+1)]
        coef_possibilities.remove(0)
        rational_options = itertools.product(*[coef_possibilities, coef_possibilities])
        rational_keys = [int((mpmath.mpf(ratio[0]) / ratio[1]) * key_factor) for ratio in rational_options]
        # +-1 for numeric errors in keys.
        rational_blacklist = set(rational_keys + [x+1 for x in rational_keys] + [x-1 for x in rational_keys])

        # create enumeration lists
        constants = [mpmath.mpf(1)] + const_vals
        coefs_top = [range(-search_range, search_range + 1)] * len(constants)  # numerator range
        coefs_bottom = [range(-search_range, search_range + 1)] * len(constants)  # denominator range
        coef_top_list = itertools.product(*coefs_top)
        coef_bottom_list = list(itertools.product(*coefs_bottom))
        denominator_list = [sum(i*j for (i, j) in zip(c_bottom, constants)) for c_bottom in coef_bottom_list]

        # start enumerating
        for c_top in coef_top_list:
            numerator = sum(i * j for (i, j) in zip(c_top, constants))
            if numerator <= 0:  # allow only positive values to avoid duplication
                continue
            numerator = mpmath.mpf(numerator)
            for c_bottom, denominator in zip(coef_bottom_list, denominator_list):
                if reduce(gcd, c_top + c_bottom) != 1:  # avoid expressions that can be simplified easily
                    continue
                if denominator == 0:  # don't store inf or nan.
                    continue
                val = numerator / denominator
                key = int(val * key_factor)
                if key in rational_blacklist:
                    # don't store values that are independent of the constant (e.g. rational numbers)
                    continue
                self.s[key] = c_top, c_bottom  # store key and transformation

    def __contains__(self, item):
        """
        operator 'in'
        :param item: key
        :return: true of false
        """
        return item in self.s

    def __getitem__(self, item):
        """
        operator []
        :param item: key
        :return: transformation of x
        """
        return self.s[item]

    def __eq__(self, other):
        """
        operator ==
        :param other: other hash table.
        :return:
        """
        if type(other) != type(self):
            return False
        ret = self.threshold == other.threshold
        ret &= sorted(self.s.keys()) == sorted(other.s.keys())
        return ret

    def save(self, name):
        """
        save the hash table as file
        :param name: path for file.
        """
        if g_hash_instance.name != name:  # save to global instance.
            g_hash_instance.hash = self
            g_hash_instance.name = name
        with open(name, 'wb') as f:
            pickle.dump(self, f)

    @classmethod
    def load_from(cls, name):
        """
        load hash table from file (or global instance)
        :param name:
        :return:
        """
        if g_hash_instance.name == name:
            print('loading instance')
            return g_hash_instance.hash  # hopefully on linux this will not make a copy.
        else:
            with open(name, 'rb') as f:
                print('not loading instance')
                ret = pickle.load(f)
                g_hash_instance.hash = ret  # save in instance
                g_hash_instance.name = name
        return ret


class EnumerateOverGCF(object):
    def __init__(self, sym_constants, lhs_search_limit, saved_hash=None,
                 an_generator: SeriesGeneratorClass = CartesianProductAnGenerator(),
                 bn_generator: SeriesGeneratorClass = CartesianProductBnGenerator()):
        """
        initialize search engine.
        basically, this is a 3 step procedure:
        1) load / initialize lhs hash table.
        2) first enumeration - enumerate over all rhs combinations, find hits in lhs hash table.
        3) refine results - take results from (2) and validate them to 100 decimal digits.
        :param sym_constants: sympy constants
        :param lhs_search_limit: range of coefficients for left hand side.
        :param saved_hash: path to saved hash.
        :param an_generator: generating function for {an} series
        :param bn_generator: generating function for {bn} series
        """
        self.threshold = 1e-10  # key length
        self.enum_dps = 50  # working decimal precision for first enumeration
        self.verify_dps = 2000  # working decimal precision for validating results.
        self.lhs_limit = lhs_search_limit
        self.const_sym = sym_constants
        self.constants_generator = []
        for i in range(len(sym_constants)):
            try:
                self.constants_generator.append(lambdify((), sym_constants[i], modules="mpmath"))
            except AttributeError:      # Hackish constant
                self.constants_generator.append(sym_constants[i].mpf_val)

        self.create_an_series = an_generator.get_function()
        self.get_an_length = an_generator.get_num_iterations
        self.get_an_iterator = an_generator.get_iterator
        self.create_bn_series = bn_generator.get_function()
        self.get_bn_length = bn_generator.get_num_iterations
        self.get_bn_iterator = bn_generator.get_iterator

        if saved_hash is None:
            print('no previous hash table given, initializing hash table...')
            with mpmath.workdps(self.enum_dps):
                constants = [const() for const in self.constants_generator]
                start = time()
                self.hash_table = LHSHashTable(
                    self.lhs_limit,
                    constants,  # constant
                    self.threshold)  # length of key
                end = time()
                print(f'that took {end-start}s')
        else:
            self.hash_table = LHSHashTable.load_from(saved_hash)

    @staticmethod
    def __create_series_list(coefficient_iter: Iterator,
                             series_generator: Callable[[List[int], int], List[int]],
                             filter_from_1=False) -> [List[int], List[int]]:
        coef_list = list(coefficient_iter)
        # create a_n and b_n series fro coefficients.
        series_list = [series_generator(coef_list[i], g_N_initial_search_terms) for i in range(len(coef_list))]
        # filter out all options resulting in '0' in any series term.
        if filter_from_1:
            series_filter = [0 not in an[1:] for an in series_list]
        else:
            series_filter = [0 not in an for an in series_list]
        series_list = list(itertools.compress(series_list, series_filter))
        coef_list = list(itertools.compress(coef_list, series_filter))
        return coef_list, series_list

    def __first_enumeration(self, poly_a: List[List], poly_b: List[List], print_results: bool):
        """
        this is usually the bottleneck of the search.
        we calculate general continued fractions of type K(bn,an). 'an' and 'bn' are polynomial series.
        these polynomials take the form of n(n(..(n*c_1 + c_0) + c_2)..)+c_k.
        poly parameters are a list of coefficients c_i. then the enumeration takes place on all possible products.
        for example, if poly_a is [[1,2],[2,3]], then the product polynomials are:
           possible [c0,c1] = { [1,2] , [1,3], [2,2], [2,3] }.
        this explodes exponentially -
        example: fs poly_a.shape = poly_b.shape = [3,5]     (2 polynomials of degree 2), then the number of
        total permutations is: (a poly possibilities) X (b poly possibilities) = (5X5X5) X (5X5X5) = 5**6
        we search on all possible gcf with polynomials defined by parameters, and try to find hits in hash table.
        :param poly_a: compact polynomial form of 'an' (list of lists)
        :param poly_b: compact polynomial form of 'an' (list of lists)
        :param print_results: if True print the status of calculation.
        :return: intermediate results (list of 'Match')
        """
        def efficient_gcf_calculation():
            """
            enclosure. a_, b_, and key_factor are used from outer scope.
            moved here from mobius.EfficientGCF to optimize performance.
            :return: key for LHS hash table
            """
            prev_q = 0
            q = 1
            prev_p = 1
            p = a_[0]
            for i in range(1, len(a_)):
                tmp_a = q
                tmp_b = p
                q = a_[i] * q + b_[i - 1] * prev_q
                p = a_[i] * p + b_[i - 1] * prev_p
                prev_q = tmp_a
                prev_p = tmp_b
            if q == 0:  # safety check
                value = 0
            else:
                value = mpmath.mpf(p) / mpmath.mpf(q)
            return int(value * key_factor)  # calculate hash key of gcf value

        start = time()
        a_coef_iter = self.get_an_iterator(poly_a)  # all coefficients possibilities for 'a_n'
        b_coef_iter = self.get_bn_iterator(poly_b)
        size_b = self.get_bn_length(poly_b)
        size_a = self.get_an_length(poly_a)
        num_iterations = size_b * size_a
        key_factor = 1 / self.threshold

        counter = 0  # number of permutations passed
        print_counter = counter
        results = []  # list of intermediate results

        if size_a > size_b:     # cache {bn} in RAM, iterate over an
            b_coef_list, bn_list = self.__create_series_list(b_coef_iter, self.create_bn_series)
            real_bn_size = len(bn_list)
            num_iterations = (num_iterations // self.get_bn_length(poly_b)) * real_bn_size
            if print_results:
                print(f'created final enumerations filters after {time() - start}s')
            start = time()
            for a_coef in a_coef_iter:
                an = self.create_an_series(a_coef, 32)
                if 0 in an[1:]:     # a_0 is allowed to be 0.
                    counter += real_bn_size
                    print_counter += real_bn_size
                    continue
                for bn_coef in zip(bn_list, b_coef_list):
                    # evaluation of GCF: taken from mobius.EfficientGCF and moved here to avoid function call overhead.
                    a_ = an
                    b_ = bn_coef[0]
                    key = efficient_gcf_calculation()  # calculate hash key of gcf value

                    if key in self.hash_table:  # find hits in hash table
                        results.append(Match(key, a_coef, bn_coef[1]))
                    if print_results:
                        counter += 1
                        print_counter += 1
                        if print_counter >= 100000:  # print status.
                            print_counter = 0
                            print(f'passed {counter} out of {num_iterations}. found so far {len(results)} results')

        else:   # cache {an} in RAM, iterate over bn
            a_coef_list, an_list = self.__create_series_list(a_coef_iter, self.create_an_series, filter_from_1=True)
            real_an_size = len(an_list)
            num_iterations = (num_iterations // self.get_an_length(poly_a)) * real_an_size
            if print_results:
                print(f'created final enumerations filters after {time() - start}s')
            start = time()
            for b_coef in b_coef_iter:
                bn = self.create_bn_series(b_coef, 32)
                if 0 in bn:
                    counter += real_an_size
                    print_counter += real_an_size
                    continue
                for an_coef in zip(an_list, a_coef_list):
                    a_ = an_coef[0]
                    b_ = bn
                    key = efficient_gcf_calculation()  # calculate hash key of gcf value

                    if key in self.hash_table:  # find hits in hash table
                        results.append(Match(key, an_coef[1], b_coef))
                    if print_results:
                        counter += 1
                        print_counter += 1
                        if print_counter >= 100000:  # print status.
                            print_counter = 0
                            print(f'passed {counter} out of {num_iterations}. found so far {len(results)} results')

        if print_results:
            print(f'created results after {time() - start}s')
        return results

    def __refine_results(self, intermediate_results: List[Match], print_results=True):
        """
        validate intermediate results to 100 digit precision
        :param intermediate_results:  list of results from first enumeration
        :param print_results: if true print status.
        :return: final results.
        """
        results = []
        counter = 0
        n_iterations = len(intermediate_results)
        constant_vals = [const() for const in self.constants_generator]
        for r in intermediate_results:
            counter += 1
            if (counter % 10) == 0 and print_results:
                print('passed {} permutations out of {}. found so far {} matches'.format(
                    counter, n_iterations, len(results)))
            try:
                val = self.hash_table.evaluate(r.lhs_key, constant_vals)
                if mpmath.isinf(val) or mpmath.isnan(val):  # safety
                    continue
            except ZeroDivisionError:
                continue

            # create a_n, b_n with huge length, calculate gcf, and verify result.
            an = self.create_an_series(r.rhs_an_poly, g_N_verify_terms)
            bn = self.create_bn_series(r.rhs_bn_poly, g_N_verify_terms)
            gcf = EfficientGCF(an, bn)
            val_str = mpmath.nstr(val, 100)
            rhs_str = mpmath.nstr(gcf.evaluate(), 100)
            if val_str == rhs_str:
                results.append(r)
        return results

    def __get_formatted_results(self, results: List[Match]) -> List[FormattedResult]:
        ret = []
        for r in results:
            an = self.create_an_series(r.rhs_an_poly, 250)
            bn = self.create_bn_series(r.rhs_bn_poly, 250)
            print_length = max(max(len(r.rhs_an_poly), len(r.rhs_bn_poly)), 5)
            gcf = GeneralizedContinuedFraction(an, bn)
            sym_lhs = self.hash_table.evaluate_sym(r.lhs_key, self.const_sym)
            ret.append(FormattedResult(sym_lhs, gcf.sym_expression(print_length), gcf))
        return ret

    def print_results(self, results: List[Match], latex=False, convergence_rate=True):
        """
        pretty print the the results.
        :param convergence_rate: if True calculate convergence rate and print it as well.
        :param results: list of final results as received from refine_results.
        :param latex: if True print in latex form, otherwise pretty print in unicode.
        """
        formatted_results = self.__get_formatted_results(results)
        for r in formatted_results:
            result = sympy.Eq(r.LHS, r.RHS)
            if latex:
                print(f'$$ {sympy.latex(result)} $$')
            else:
                sympy.pprint(result)
            if convergence_rate:
                with mpmath.workdps(self.verify_dps):
                    rate = calculate_convergence(r.GCF, lambdify((), r.LHS, 'mpmath')())
                print("Converged with a rate of {} digits per term".format(mpmath.nstr(rate, 5)))

    def convert_results_to_latex(self, results: List[Match]):
        results_in_latex = []
        formatted_results = self.__get_formatted_results(results)
        for r in formatted_results:
            equation = sympy.Eq(r.LHS, r.RHS)
            results_in_latex.append(sympy.latex(equation))
        return results_in_latex

    def find_hits(self, poly_a: List[List], poly_b: List[List], print_results=True):
        """
        use search engine to find results (steps (2) and (3) explained in __init__ docstring)
        :param poly_a: explained in docstring of __first_enumeration
        :param poly_b: explained in docstring of __first_enumeration
        :param print_results: if true, pretty print results at the end.
        :return: final results.
        """
        with mpmath.workdps(self.enum_dps):
            if print_results:
                print('starting preliminary search...')
            start = time()
            # step (2)
            results = self.__first_enumeration(poly_a, poly_b, print_results)
            end = time()
            if print_results:
                print(f'that took {end - start}s')
        with mpmath.workdps(self.verify_dps*2):
            if print_results:
                print('starting to verify results...')
            start = time()
            refined_results = self.__refine_results(results, print_results)  # step (3)
            end = time()
            if print_results:
                print(f'that took {end - start}s')
        return refined_results


def multi_core_enumeration(sym_constant, lhs_search_limit, saved_hash, poly_a, poly_b, num_cores, splits_size,
                           create_an_series=None, create_bn_series=None, index=0):
    """
    function to run for each process. this also divides the work to tiles/
    :param sym_constant: sympy constant for search
    :param lhs_search_limit:  limit for hash table
    :param saved_hash: path to saved hash table
    :param poly_a: explained in docstring of __first_enumeration
    :param poly_b: explained in docstring of __first_enumeration
    :param num_cores: total number of cores used.
    :param splits_size: tile size for each process.
    we can think of the search domain as a n-d array with dim(poly_a) + dim(poly_b) dimensions.
    to split this efficiently we need the tile size. for each value in splits_size we take it as the tile size for a
    dimension of the search domain. for example, is split size is [4,5] then we will split the work in the first
    2 dimensions of the search domain to tiles of size [4,5].
    NOTICE - we do not verify that the tile size make sense to the number of cores used.
    :param index: index of core used.
    :param create_an_series: a custom function for creating a_n series with poly_a coefficients
    (default is create_series_from_compact_poly)
    :param create_bn_series: a custom function for creating b_n series with poly_b coefficients
    (default is create_series_from_compact_poly)
    :return: results
    """
    for s in range(len(splits_size)):
        if index == (num_cores - 1):  # last processor does more.
            poly_a[s] = poly_a[s][index * splits_size[s]:]
        else:
            poly_a[s] = poly_a[s][index * splits_size[s]:(index + 1) * splits_size[s]]

    enumerator = EnumerateOverGCF(sym_constant, lhs_search_limit, saved_hash, create_an_series, create_bn_series)

    results = enumerator.find_hits(poly_a, poly_b, index == (num_cores - 1))

    return results


def multi_core_enumeration_wrapper(sym_constant, lhs_search_limit, poly_a, poly_b, num_cores, manual_splits_size=None,
                                   saved_hash=None, create_an_series=None, create_bn_series=None):
    """
    a wrapper for enumerating using multi processing.
    :param sym_constant: sympy constant for search
    :param lhs_search_limit: limit for hash table
    :param poly_a: explained in docstring of __first_enumeration
    :param poly_b: explained in docstring of __first_enumeration
    :param num_cores: total number of cores to be used.
    :param manual_splits_size: amount of work for each processor.
    manuals tiling (explained in docstring of multi_core_enumeration)
    by default we will split the work only along the first dimension. so the tile size will be
    [dim0 / n_cores, . , . , . , rest of dimensions].
    passing this manually can be useful for a large number of cores.
    :param saved_hash: path to saved hash table file if exists.
    :param create_an_series: a custom function for creating a_n series with poly_a coefficients
    (default is create_series_from_compact_poly)
    :param create_bn_series: a custom function for creating b_n series with poly_b coefficients
    (default is create_series_from_compact_poly)
    :return: results.
    """
    print(locals())
    if (saved_hash is None) or (not os.path.isfile(saved_hash)):
        if saved_hash is None:  # if no hash table given, build it here.
            saved_hash = 'tmp_hash.p'
        enumerator = EnumerateOverGCF(sym_constant, lhs_search_limit)
        enumerator.hash_table.save(saved_hash)  # and save it to file (and global instance)
    else:
        if os.name != 'nt':  # if creation of process uses 'Copy On Write' we can benefit from it by
            # loading the hash table to memory here.
            EnumerateOverGCF(sym_constant, lhs_search_limit, saved_hash)

    if manual_splits_size is None:  # naive work split
        manual_splits_size = [len(poly_a[0]) // num_cores]

    # built function for processes
    func = partial(multi_core_enumeration, sym_constant, lhs_search_limit, saved_hash, poly_a, poly_b, num_cores,
                   manual_splits_size, create_an_series, create_bn_series)

    if num_cores == 1:  # don't open child processes
        results = func(0)
        print(f'found {len(results)} results!')
    else:
        print('starting Multi-Processor search.\n\tNOTICE- intermediate status prints will be done by processor 0 only.')
        pool = multiprocessing.Pool(num_cores)
        partial_results = pool.map(func, range(num_cores))
        results = []
        for r in partial_results:
            results += r
        print(f'found {len(results)} results!')

    print('preparing results...')
    enumerator = EnumerateOverGCF(sym_constant, lhs_search_limit, saved_hash, create_an_series, create_bn_series)
    print('results in unicode:')
    enumerator.print_results(results, latex=False, convergence_rate=False)
    print('results in latex:')
    enumerator.print_results(results, latex=True, convergence_rate=False)

    results_in_latex = enumerator.convert_results_to_latex(results)
    generate_latex(file_name=f'results/{datetime.now().strftime("%m-%d-%Y--%H-%M-%S")}', eqns=results_in_latex)

    return results_in_latex
