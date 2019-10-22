""" discover and run doctests in modules and test files."""
import bdb
import inspect
import platform
import sys
import traceback
import warnings
from contextlib import contextmanager

import pytest
from _pytest import outcomes
from _pytest._code.code import ExceptionInfo
from _pytest._code.code import ReprFileLocation
from _pytest._code.code import TerminalRepr
from _pytest.compat import safe_getattr
from _pytest.fixtures import FixtureRequest
from _pytest.outcomes import Skipped
from _pytest.python_api import approx
from _pytest.warning_types import PytestWarning

DOCTEST_REPORT_CHOICE_NONE = "none"
DOCTEST_REPORT_CHOICE_CDIFF = "cdiff"
DOCTEST_REPORT_CHOICE_NDIFF = "ndiff"
DOCTEST_REPORT_CHOICE_UDIFF = "udiff"
DOCTEST_REPORT_CHOICE_ONLY_FIRST_FAILURE = "only_first_failure"

DOCTEST_REPORT_CHOICES = (
    DOCTEST_REPORT_CHOICE_NONE,
    DOCTEST_REPORT_CHOICE_CDIFF,
    DOCTEST_REPORT_CHOICE_NDIFF,
    DOCTEST_REPORT_CHOICE_UDIFF,
    DOCTEST_REPORT_CHOICE_ONLY_FIRST_FAILURE,
)

# Lazy definition of runner class
RUNNER_CLASS = None


def pytest_addoption(parser):
    parser.addini(
        "doctest_optionflags",
        "option flags for doctests",
        type="args",
        default=["ELLIPSIS"],
    )
    parser.addini(
        "doctest_encoding", "encoding used for doctest files", default="utf-8"
    )
    group = parser.getgroup("collect")
    group.addoption(
        "--doctest-modules",
        action="store_true",
        default=False,
        help="run doctests in all .py modules",
        dest="doctestmodules",
    )
    group.addoption(
        "--doctest-report",
        type=str.lower,
        default="udiff",
        help="choose another output format for diffs on doctest failure",
        choices=DOCTEST_REPORT_CHOICES,
        dest="doctestreport",
    )
    group.addoption(
        "--doctest-glob",
        action="append",
        default=[],
        metavar="pat",
        help="doctests file matching pattern, default: test*.txt",
        dest="doctestglob",
    )
    group.addoption(
        "--doctest-ignore-import-errors",
        action="store_true",
        default=False,
        help="ignore doctest ImportErrors",
        dest="doctest_ignore_import_errors",
    )
    group.addoption(
        "--doctest-continue-on-failure",
        action="store_true",
        default=False,
        help="for a given doctest, continue to run after the first failure",
        dest="doctest_continue_on_failure",
    )


def pytest_collect_file(path, parent):
    config = parent.config
    if path.ext == ".py":
        if config.option.doctestmodules and not _is_setup_py(config, path, parent):
            return DoctestModule(path, parent)
    elif _is_doctest(config, path, parent):
        return DoctestTextfile(path, parent)


def _is_setup_py(config, path, parent):
    if path.basename != "setup.py":
        return False
    contents = path.read()
    return "setuptools" in contents or "distutils" in contents


def _is_doctest(config, path, parent):
    if path.ext in (".txt", ".rst") and parent.session.isinitpath(path):
        return True
    globs = config.getoption("doctestglob") or ["test*.txt"]
    for glob in globs:
        if path.check(fnmatch=glob):
            return True
    return False


class ReprFailDoctest(TerminalRepr):
    def __init__(self, reprlocation_lines):
        # List of (reprlocation, lines) tuples
        self.reprlocation_lines = reprlocation_lines

    def toterminal(self, tw):
        for reprlocation, lines in self.reprlocation_lines:
            for line in lines:
                tw.line(line)
            reprlocation.toterminal(tw)


class MultipleDoctestFailures(Exception):
    def __init__(self, failures):
        super().__init__()
        self.failures = failures


def _init_runner_class():
    import doctest

    class PytestDoctestRunner(doctest.DebugRunner):
        """
        Runner to collect failures.  Note that the out variable in this case is
        a list instead of a stdout-like object
        """

        def __init__(
            self, checker=None, verbose=None, optionflags=0, continue_on_failure=True
        ):
            doctest.DebugRunner.__init__(
                self, checker=checker, verbose=verbose, optionflags=optionflags
            )
            self.continue_on_failure = continue_on_failure

        def report_failure(self, out, test, example, got):
            failure = doctest.DocTestFailure(test, example, got)
            if self.continue_on_failure:
                out.append(failure)
            else:
                raise failure

        def report_unexpected_exception(self, out, test, example, exc_info):
            if isinstance(exc_info[1], Skipped):
                raise exc_info[1]
            if isinstance(exc_info[1], bdb.BdbQuit):
                outcomes.exit("Quitting debugger")
            failure = doctest.UnexpectedException(test, example, exc_info)
            if self.continue_on_failure:
                out.append(failure)
            else:
                raise failure

    return PytestDoctestRunner


def _get_runner(checker=None, verbose=None, optionflags=0, continue_on_failure=True):
    # We need this in order to do a lazy import on doctest
    global RUNNER_CLASS
    if RUNNER_CLASS is None:
        RUNNER_CLASS = _init_runner_class()
    return RUNNER_CLASS(
        checker=checker,
        verbose=verbose,
        optionflags=optionflags,
        continue_on_failure=continue_on_failure,
    )


class DoctestItem(pytest.Item):
    def __init__(self, name, parent, runner=None, dtest=None):
        super().__init__(name, parent)
        self.runner = runner
        self.dtest = dtest
        self.obj = None
        self.fixture_request = None

    def setup(self):
        if self.dtest is not None:
            self.fixture_request = _setup_fixtures(self)
            globs = dict(getfixture=self.fixture_request.getfixturevalue)
            for name, value in self.fixture_request.getfixturevalue(
                "doctest_namespace"
            ).items():
                globs[name] = value
            self.dtest.globs.update(globs)

    def runtest(self):
        _check_all_skipped(self.dtest)
        self._disable_output_capturing_for_darwin()
        failures = []
        self.runner.run(self.dtest, out=failures)
        if failures:
            raise MultipleDoctestFailures(failures)

    def _disable_output_capturing_for_darwin(self):
        """
        Disable output capturing. Otherwise, stdout is lost to doctest (#985)
        """
        if platform.system() != "Darwin":
            return
        capman = self.config.pluginmanager.getplugin("capturemanager")
        if capman:
            capman.suspend_global_capture(in_=True)
            out, err = capman.read_global_capture()
            sys.stdout.write(out)
            sys.stderr.write(err)

    def repr_failure(self, excinfo):
        import doctest

        failures = None
        if excinfo.errisinstance((doctest.DocTestFailure, doctest.UnexpectedException)):
            failures = [excinfo.value]
        elif excinfo.errisinstance(MultipleDoctestFailures):
            failures = excinfo.value.failures

        if failures is not None:
            reprlocation_lines = []
            for failure in failures:
                example = failure.example
                test = failure.test
                filename = test.filename
                if test.lineno is None:
                    lineno = None
                else:
                    lineno = test.lineno + example.lineno + 1
                message = type(failure).__name__
                reprlocation = ReprFileLocation(filename, lineno, message)
                checker = _get_checker()
                report_choice = _get_report_choice(
                    self.config.getoption("doctestreport")
                )
                if lineno is not None:
                    lines = failure.test.docstring.splitlines(False)
                    # add line numbers to the left of the error message
                    lines = [
                        "%03d %s" % (i + test.lineno + 1, x)
                        for (i, x) in enumerate(lines)
                    ]
                    # trim docstring error lines to 10
                    lines = lines[max(example.lineno - 9, 0) : example.lineno + 1]
                else:
                    lines = [
                        "EXAMPLE LOCATION UNKNOWN, not showing all tests of that example"
                    ]
                    indent = ">>>"
                    for line in example.source.splitlines():
                        lines.append("??? {} {}".format(indent, line))
                        indent = "..."
                if isinstance(failure, doctest.DocTestFailure):
                    lines += checker.output_difference(
                        example, failure.got, report_choice
                    ).split("\n")
                else:
                    inner_excinfo = ExceptionInfo(failure.exc_info)
                    lines += ["UNEXPECTED EXCEPTION: %s" % repr(inner_excinfo.value)]
                    lines += traceback.format_exception(*failure.exc_info)
                reprlocation_lines.append((reprlocation, lines))
            return ReprFailDoctest(reprlocation_lines)
        else:
            return super().repr_failure(excinfo)

    def reportinfo(self):
        return self.fspath, self.dtest.lineno, "[doctest] %s" % self.name


def _get_flag_lookup():
    import doctest

    return dict(
        DONT_ACCEPT_TRUE_FOR_1=doctest.DONT_ACCEPT_TRUE_FOR_1,
        DONT_ACCEPT_BLANKLINE=doctest.DONT_ACCEPT_BLANKLINE,
        NORMALIZE_WHITESPACE=doctest.NORMALIZE_WHITESPACE,
        ELLIPSIS=doctest.ELLIPSIS,
        IGNORE_EXCEPTION_DETAIL=doctest.IGNORE_EXCEPTION_DETAIL,
        COMPARISON_FLAGS=doctest.COMPARISON_FLAGS,
        ALLOW_UNICODE=_get_allow_unicode_flag(),
        ALLOW_BYTES=_get_allow_bytes_flag(),
        NUMBER=_get_number_flag(),
    )


def get_optionflags(parent):
    optionflags_str = parent.config.getini("doctest_optionflags")
    flag_lookup_table = _get_flag_lookup()
    flag_acc = 0
    for flag in optionflags_str:
        flag_acc |= flag_lookup_table[flag]
    return flag_acc


def _get_continue_on_failure(config):
    continue_on_failure = config.getvalue("doctest_continue_on_failure")
    if continue_on_failure:
        # We need to turn off this if we use pdb since we should stop at
        # the first failure
        if config.getvalue("usepdb"):
            continue_on_failure = False
    return continue_on_failure


class DoctestTextfile(pytest.Module):
    obj = None

    def collect(self):
        import doctest

        # inspired by doctest.testfile; ideally we would use it directly,
        # but it doesn't support passing a custom checker
        encoding = self.config.getini("doctest_encoding")
        text = self.fspath.read_text(encoding)
        filename = str(self.fspath)
        name = self.fspath.basename
        globs = {"__name__": "__main__"}

        optionflags = get_optionflags(self)

        runner = _get_runner(
            verbose=0,
            optionflags=optionflags,
            checker=_get_checker(),
            continue_on_failure=_get_continue_on_failure(self.config),
        )

        parser = doctest.DocTestParser()
        test = parser.get_doctest(text, globs, name, filename, 0)
        if test.examples:
            yield DoctestItem(test.name, self, runner, test)


def _check_all_skipped(test):
    """raises pytest.skip() if all examples in the given DocTest have the SKIP
    option set.
    """
    import doctest

    all_skipped = all(x.options.get(doctest.SKIP, False) for x in test.examples)
    if all_skipped:
        pytest.skip("all tests skipped by +SKIP option")


def _is_mocked(obj):
    """
    returns if a object is possibly a mock object by checking the existence of a highly improbable attribute
    """
    return (
        safe_getattr(obj, "pytest_mock_example_attribute_that_shouldnt_exist", None)
        is not None
    )


@contextmanager
def _patch_unwrap_mock_aware():
    """
    contextmanager which replaces ``inspect.unwrap`` with a version
    that's aware of mock objects and doesn't recurse on them
    """
    real_unwrap = inspect.unwrap

    def _mock_aware_unwrap(obj, stop=None):
        try:
            if stop is None or stop is _is_mocked:
                return real_unwrap(obj, stop=_is_mocked)
            return real_unwrap(obj, stop=lambda obj: _is_mocked(obj) or stop(obj))
        except Exception as e:
            warnings.warn(
                "Got %r when unwrapping %r.  This is usually caused "
                "by a violation of Python's object protocol; see e.g. "
                "https://github.com/pytest-dev/pytest/issues/5080" % (e, obj),
                PytestWarning,
            )
            raise

    inspect.unwrap = _mock_aware_unwrap
    try:
        yield
    finally:
        inspect.unwrap = real_unwrap


class DoctestModule(pytest.Module):
    def collect(self):
        import doctest

        class MockAwareDocTestFinder(doctest.DocTestFinder):
            """
            a hackish doctest finder that overrides stdlib internals to fix a stdlib bug

            https://github.com/pytest-dev/pytest/issues/3456
            https://bugs.python.org/issue25532
            """

            def _find(self, tests, obj, name, module, source_lines, globs, seen):
                if _is_mocked(obj):
                    return
                with _patch_unwrap_mock_aware():

                    doctest.DocTestFinder._find(
                        self, tests, obj, name, module, source_lines, globs, seen
                    )

        if self.fspath.basename == "conftest.py":
            module = self.config.pluginmanager._importconftest(self.fspath)
        else:
            try:
                module = self.fspath.pyimport()
            except ImportError:
                if self.config.getvalue("doctest_ignore_import_errors"):
                    pytest.skip("unable to import module %r" % self.fspath)
                else:
                    raise
        # uses internal doctest module parsing mechanism
        finder = MockAwareDocTestFinder()
        optionflags = get_optionflags(self)
        runner = _get_runner(
            verbose=0,
            optionflags=optionflags,
            checker=_get_checker(),
            continue_on_failure=_get_continue_on_failure(self.config),
        )

        for test in finder.find(module, module.__name__):
            if test.examples:  # skip empty doctests
                yield DoctestItem(test.name, self, runner, test)


def _setup_fixtures(doctest_item):
    """
    Used by DoctestTextfile and DoctestItem to setup fixture information.
    """

    def func():
        pass

    doctest_item.funcargs = {}
    fm = doctest_item.session._fixturemanager
    doctest_item._fixtureinfo = fm.getfixtureinfo(
        node=doctest_item, func=func, cls=None, funcargs=False
    )
    fixture_request = FixtureRequest(doctest_item)
    fixture_request._fillfixtures()
    return fixture_request


def _get_checker():
    """
    Returns a doctest.OutputChecker subclass that supports some
    additional options:

    * ALLOW_UNICODE and ALLOW_BYTES options to ignore u'' and b''
      prefixes (respectively) in string literals. Useful when the same
      doctest should run in Python 2 and Python 3.

    * NUMBER to ignore floating-point differences smaller than the
      precision of the literal number in the doctest.

    An inner class is used to avoid importing "doctest" at the module
    level.
    """
    if hasattr(_get_checker, "LiteralsOutputChecker"):
        return _get_checker.LiteralsOutputChecker()

    import doctest
    import re

    class LiteralsOutputChecker(doctest.OutputChecker):
        """
        Based on doctest_nose_plugin.py from the nltk project
        (https://github.com/nltk/nltk) and on the "numtest" doctest extension
        by Sebastien Boisgerault (https://github.com/boisgera/numtest).
        """

        _unicode_literal_re = re.compile(r"(\W|^)[uU]([rR]?[\'\"])", re.UNICODE)
        _bytes_literal_re = re.compile(r"(\W|^)[bB]([rR]?[\'\"])", re.UNICODE)
        _number_re = re.compile(
            r"""
            (?P<number>
              (?P<mantissa>
                (?P<integer1> [+-]?\d*)\.(?P<fraction>\d+)
                |
                (?P<integer2> [+-]?\d+)\.
              )
              (?:
                [Ee]
                (?P<exponent1> [+-]?\d+)
              )?
              |
              (?P<integer3> [+-]?\d+)
              (?:
                [Ee]
                (?P<exponent2> [+-]?\d+)
              )
            )
            """,
            re.VERBOSE,
        )

        def check_output(self, want, got, optionflags):
            if doctest.OutputChecker.check_output(self, want, got, optionflags):
                return True

            allow_unicode = optionflags & _get_allow_unicode_flag()
            allow_bytes = optionflags & _get_allow_bytes_flag()
            allow_number = optionflags & _get_number_flag()

            if not allow_unicode and not allow_bytes and not allow_number:
                return False

            def remove_prefixes(regex, txt):
                return re.sub(regex, r"\1\2", txt)

            if allow_unicode:
                want = remove_prefixes(self._unicode_literal_re, want)
                got = remove_prefixes(self._unicode_literal_re, got)

            if allow_bytes:
                want = remove_prefixes(self._bytes_literal_re, want)
                got = remove_prefixes(self._bytes_literal_re, got)

            if allow_number:
                got = self._remove_unwanted_precision(want, got)

            return doctest.OutputChecker.check_output(self, want, got, optionflags)

        def _remove_unwanted_precision(self, want, got):
            wants = list(self._number_re.finditer(want))
            gots = list(self._number_re.finditer(got))
            if len(wants) != len(gots):
                return got
            offset = 0
            for w, g in zip(wants, gots):
                fraction = w.group("fraction")
                exponent = w.group("exponent1")
                if exponent is None:
                    exponent = w.group("exponent2")
                if fraction is None:
                    precision = 0
                else:
                    precision = len(fraction)
                if exponent is not None:
                    precision -= int(exponent)
                if float(w.group()) == approx(float(g.group()), abs=10 ** -precision):
                    # They're close enough. Replace the text we actually
                    # got with the text we want, so that it will match when we
                    # check the string literally.
                    got = (
                        got[: g.start() + offset] + w.group() + got[g.end() + offset :]
                    )
                    offset += w.end() - w.start() - (g.end() - g.start())
            return got

    _get_checker.LiteralsOutputChecker = LiteralsOutputChecker
    return _get_checker.LiteralsOutputChecker()


def _get_allow_unicode_flag():
    """
    Registers and returns the ALLOW_UNICODE flag.
    """
    import doctest

    return doctest.register_optionflag("ALLOW_UNICODE")


def _get_allow_bytes_flag():
    """
    Registers and returns the ALLOW_BYTES flag.
    """
    import doctest

    return doctest.register_optionflag("ALLOW_BYTES")


def _get_number_flag():
    """
    Registers and returns the NUMBER flag.
    """
    import doctest

    return doctest.register_optionflag("NUMBER")


def _get_report_choice(key):
    """
    This function returns the actual `doctest` module flag value, we want to do it as late as possible to avoid
    importing `doctest` and all its dependencies when parsing options, as it adds overhead and breaks tests.
    """
    import doctest

    return {
        DOCTEST_REPORT_CHOICE_UDIFF: doctest.REPORT_UDIFF,
        DOCTEST_REPORT_CHOICE_CDIFF: doctest.REPORT_CDIFF,
        DOCTEST_REPORT_CHOICE_NDIFF: doctest.REPORT_NDIFF,
        DOCTEST_REPORT_CHOICE_ONLY_FIRST_FAILURE: doctest.REPORT_ONLY_FIRST_FAILURE,
        DOCTEST_REPORT_CHOICE_NONE: 0,
    }[key]


@pytest.fixture(scope="session")
def doctest_namespace():
    """
    Fixture that returns a :py:class:`dict` that will be injected into the namespace of doctests.
    """
    return dict()
