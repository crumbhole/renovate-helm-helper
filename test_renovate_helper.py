'''
Tests for the renovate_helper PR status-check feature.

The script has no .py extension and runs its bootstrap under an
`if __name__ == '__main__'` guard, so we load it by path with importlib and
exercise the functions directly, injecting module globals (repo/api/...) as
needed. Run inside the published image which has ghapi, GitPython, git and
patch available:

    docker run --rm -v "$PWD":/work -w /work renovate-helper \
        python3 test_renovate_helper.py
'''
import importlib.util
import os
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

from git import Repo


def load_helper():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "renovate_helper")
    # The script has no .py extension, so spell out the source loader.
    loader = SourceFileLoader("renovate_helper", path)
    spec = importlib.util.spec_from_loader("renovate_helper", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)   # guard keeps main() from running
    return mod


rhh = load_helper()


class FakeRepos:
    def __init__(self, calls):
        self.calls = calls

    def create_commit_status(self, **kwargs):
        self.calls.append(kwargs)


class FakeApi:
    def __init__(self):
        self.status_calls = []
        self.repos = FakeRepos(self.status_calls)


def init_repo(path):
    repo = Repo.init(path)
    cw = repo.config_writer()
    cw.set_value("user", "name", "tester")
    cw.set_value("user", "email", "tester@example.com")
    cw.release()
    return repo


class SetCommitStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = init_repo(self.tmp)
        # need at least one commit so repo.head.commit resolves
        open(os.path.join(self.tmp, "f"), "w").close()
        self.repo.index.add(["f"])
        self.repo.index.commit("init")
        self.api = FakeApi()
        rhh.repo = self.repo
        rhh.api = self.api

    def test_noop_when_context_unset(self):
        rhh.status_context = ""
        rhh.set_commit_status("success", "hi")
        self.assertEqual(self.api.status_calls, [])

    def test_posts_on_head_when_enabled(self):
        rhh.status_context = "renovate-helm-helper"
        rhh.set_commit_status("failure", "needs merge")
        self.assertEqual(len(self.api.status_calls), 1)
        call = self.api.status_calls[0]
        self.assertEqual(call["sha"], self.repo.head.commit.hexsha)
        self.assertEqual(call["state"], "failure")
        self.assertEqual(call["context"], "renovate-helm-helper")
        self.assertEqual(call["description"], "needs merge")


class RejectCommentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        rhh.checkoutPath = self.tmp

    def _mkchart(self, name, with_rej):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        if with_rej:
            with open(os.path.join(d, "values.yaml.rej"), "w") as f:
                f.write("--- a hunk that failed\n")
        return name

    def test_clean_is_success(self):
        c = self._mkchart("clean", with_rej=False)
        comment, failed = rhh.reject_comment([c])
        self.assertEqual(comment, "")
        self.assertFalse(failed)

    def test_rej_is_failure(self):
        c = self._mkchart("dirty", with_rej=True)
        comment, failed = rhh.reject_comment([c])
        self.assertTrue(failed)
        self.assertIn("a hunk that failed", comment)
        self.assertIn("dirty", comment)   # chart dir labelled

    def test_mixed_is_failure(self):
        clean = self._mkchart("ok", with_rej=False)
        dirty = self._mkchart("bad", with_rej=True)
        comment, failed = rhh.reject_comment([clean, dirty])
        self.assertTrue(failed)
        self.assertIn("bad", comment)


class UpdateValuesFailedPatchTests(unittest.TestCase):
    '''The clean-commit guarantee: a failed patch commits nothing — the .rej is
    never staged and the half-patched values.yaml is discarded so the working
    tree stays at the merge base. The .rej is left on disk only so this run's
    comment / commit-status scan can see it; the next run regenerates it.'''
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = init_repo(self.tmp)
        rhh.repo = self.repo
        rhh.checkoutPath = self.tmp

    def test_failed_patch_commits_nothing_but_leaves_rej_on_disk(self):
        chart = "mychart"
        d = os.path.join(self.tmp, chart)
        os.makedirs(d)
        # values.yaml is heavily customised so an upstream hunk won't apply.
        values = "key: MY_CUSTOM_VALUE\ncommon: line\n"
        orig_old = "key: original\ncommon: line\n"
        for name, content in (("values.yaml", values),
                              ("orig-values.yaml", orig_old)):
            with open(os.path.join(d, name), "w") as f:
                f.write(content)
        self.repo.index.add([f"{chart}/values.yaml",
                             f"{chart}/orig-values.yaml"])
        merge_base = self.repo.index.commit("base").hexsha

        # Upstream orig-values changes the line whose context is missing from
        # the customised values.yaml -> patch rejects. values.yaml left as the
        # merge-base version so update_values takes the patch branch.
        with open(os.path.join(d, "orig-values.yaml"), "w") as f:
            f.write("key: changed\ncommon: line\n")

        rhh.update_values(chart, merge_base)

        staged = {k[0] for k in self.repo.index.entries.keys()}
        self.assertNotIn(f"{chart}/values.yaml.rej", staged,
                         "the .rej must never reach the index")
        # Nothing the helper did here may be committable: the index must still
        # match the merge base (update_orig_values stages orig-values.yaml
        # separately; update_values itself must stage nothing on a failed patch).
        self.assertEqual([], self.repo.index.diff(merge_base),
                         "a failed patch must leave a clean index")
        # The partial patch is discarded — working tree back at the merge base.
        with open(os.path.join(d, "values.yaml")) as f:
            self.assertEqual(values, f.read())
        # But the .rej is still on disk for this run's comment/status scan.
        self.assertTrue(
            os.path.exists(os.path.join(d, "values.yaml.rej")),
            "the .rej must remain on disk so reject_comment can see it")

    def test_clean_patch_leaves_no_rej(self):
        chart = "mychart"
        d = os.path.join(self.tmp, chart)
        os.makedirs(d)
        # values.yaml mirrors upstream, so the upstream hunk applies cleanly.
        base = "key: original\ncommon: line\n"
        for name in ("values.yaml", "orig-values.yaml"):
            with open(os.path.join(d, name), "w") as f:
                f.write(base)
        self.repo.index.add([f"{chart}/values.yaml",
                             f"{chart}/orig-values.yaml"])
        merge_base = self.repo.index.commit("base").hexsha

        with open(os.path.join(d, "orig-values.yaml"), "w") as f:
            f.write("key: changed\ncommon: line\n")

        rhh.update_values(chart, merge_base)

        staged = {k[0] for k in self.repo.index.entries.keys()}
        self.assertNotIn(f"{chart}/values.yaml.rej", staged)
        self.assertFalse(
            os.path.exists(os.path.join(d, "values.yaml.rej")))


class UpdateValuesNewChartTests(unittest.TestCase):
    '''A chart whose orig-values.yaml is new on this branch (absent at the
    merge base) must not crash the run: git show <merge_base>:orig-values.yaml
    exits 128 for a path that never existed there.'''
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = init_repo(self.tmp)
        rhh.repo = self.repo
        rhh.checkoutPath = self.tmp

    def test_missing_orig_values_at_merge_base_is_skipped(self):
        chart = "newchart"
        d = os.path.join(self.tmp, chart)
        os.makedirs(d)
        # Only values.yaml existed at the merge base.
        with open(os.path.join(d, "values.yaml"), "w") as f:
            f.write("key: value\n")
        self.repo.index.add([f"{chart}/values.yaml"])
        merge_base = self.repo.index.commit("base").hexsha
        # orig-values.yaml appears only now (as update_orig_values would leave
        # it), untracked at the merge base.
        with open(os.path.join(d, "orig-values.yaml"), "w") as f:
            f.write("  key: value\n")

        # Must not raise, and must not leave a .rej behind.
        rhh.update_values(chart, merge_base)

        self.assertFalse(
            os.path.exists(os.path.join(d, "values.yaml.rej")))


class TruncateCommentTests(unittest.TestCase):
    def test_short_body_unchanged(self):
        body = "a small comment"
        self.assertEqual(rhh.truncate_comment(body), body)

    def test_long_body_capped_with_notice(self):
        body = "x" * (rhh.MAX_COMMENT_CHARS + 5000)
        out = rhh.truncate_comment(body)
        self.assertLessEqual(len(out), rhh.MAX_COMMENT_CHARS)
        self.assertIn("truncated", out.lower())
        self.assertTrue(out.endswith("\n"))


class MainFlowTests(unittest.TestCase):
    '''main() maps merge outcome to the right commit-status state.'''
    def setUp(self):
        self._orig = {name: getattr(rhh, name) for name in (
            "process_diff", "try_commit_push", "comment_if_needed",
            "comment_values_update", "reject_comment", "set_commit_status")}
        self.status = []
        rhh.try_commit_push = lambda: None
        rhh.comment_if_needed = lambda c: None
        rhh.comment_values_update = lambda c: None
        rhh.set_commit_status = lambda state, desc: self.status.append(state)

    def tearDown(self):
        for name, fn in self._orig.items():
            setattr(rhh, name, fn)

    def test_clean_run_reports_success(self):
        rhh.process_diff = lambda: ("body", ["chart"])
        rhh.reject_comment = lambda dirs: ("", False)
        rhh.main()
        self.assertEqual(self.status, ["success"])

    def test_rejects_report_failure(self):
        rhh.process_diff = lambda: ("body", ["chart"])
        rhh.reject_comment = lambda dirs: ("rej comment", True)
        rhh.main()
        self.assertEqual(self.status, ["failure"])

    def test_crash_reports_error_and_reraises(self):
        def boom():
            raise RuntimeError("kaboom")
        rhh.process_diff = boom
        with self.assertRaises(RuntimeError):
            rhh.main()
        self.assertEqual(self.status, ["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
