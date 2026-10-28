"""Deterministic cancellation controls, independent of host process-table access."""

from contextlib import ExitStack
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import board_process_truth as bpt  # noqa: E402


def identity(pid, pgid=None):
    return {
        "pid": pid,
        "pgid": pid if pgid is None else pgid,
        "process_start_token": f"proc:{pid}",
        "argv_sha256": "a" * 64,
    }


class SignalIdentityTests(unittest.TestCase):
    def test_exact_identity_and_pgid_drift_both_signal_the_same_pid(self):
        recorded = identity(4100)
        for pgid in (4100, 4200):
            for signum in (signal.SIGSTOP, signal.SIGTERM, signal.SIGCONT, signal.SIGKILL):
                with (
                    self.subTest(pgid=pgid, signum=signum),
                    mock.patch.object(bpt, "observe_process", return_value={**recorded, "pgid": pgid}),
                    mock.patch.object(bpt.os, "kill") as kill,
                    mock.patch.object(bpt.os, "killpg") as killpg,
                ):
                    self.assertTrue(bpt._signal_identity(
                        recorded, signum, target="descendant", operation="terminate"
                    ))
                    kill.assert_called_once_with(4100, signum)
                    killpg.assert_not_called()

    def test_changed_incarnation_refuses_signal_even_when_pgid_also_drifted(self):
        recorded = identity(4100)
        for field, changed in (
            ("pid", 4200), ("process_start_token", "proc:replacement"),
            ("argv_sha256", "b" * 64),
        ):
            with (
                self.subTest(field=field),
                mock.patch.object(bpt, "observe_process", return_value={
                    **recorded, "pgid": 4300, field: changed,
                }),
                mock.patch.object(bpt.os, "kill") as kill,
                mock.patch.object(bpt.os, "killpg") as killpg,
            ):
                with self.assertRaisesRegex(bpt.ProcessTruthError, "process identity changed") as caught:
                    bpt._signal_identity(recorded, signal.SIGKILL,
                                         target="descendant", operation="terminate")
                self.assertIn(field, str(caught.exception).split("mismatches: ")[1])
                kill.assert_not_called()
                killpg.assert_not_called()

    def test_exit_between_observation_and_signal_is_not_a_reused_pid(self):
        recorded = identity(4100)
        with (
            mock.patch.object(bpt, "observe_process", return_value=recorded),
            mock.patch.object(bpt.os, "kill", side_effect=ProcessLookupError),
        ):
            self.assertFalse(bpt._signal_identity(
                recorded, signal.SIGCONT, target="descendant", operation="continue"
            ))

    def test_reused_tree_root_is_refused_before_any_signal(self):
        recorded = identity(4100)
        with (
            mock.patch.object(bpt.os, "getsid", return_value=4100),
            mock.patch.object(bpt, "observe_process", return_value={
                **recorded, "process_start_token": "proc:replacement",
            }),
            mock.patch.object(bpt.os, "kill") as kill,
            mock.patch.object(bpt.os, "killpg") as killpg,
        ):
            with self.assertRaisesRegex(bpt.ProcessTruthError, "process_start_token"):
                bpt.terminate_attributable_tree(recorded, grace=0)
            kill.assert_not_called()
            killpg.assert_not_called()


class TreeTerminationTests(unittest.TestCase):
    def setUp(self):
        self.root, self.worker, self.child = identity(4100), identity(4200), identity(4300, 4200)
        self.members = [self.root, self.worker, self.child]
        self.live = {item["pid"]: dict(item) for item in self.members}
        self.parents = {4100: 1, 4200: 4100, 4300: 4200}
        self.now = 0.0
        self.delivered = []
        self.on_signal = lambda _pid, _sig: None
        self.on_sleep = lambda: None
        stack = self.enterContext(ExitStack())
        stack.enter_context(mock.patch.object(bpt.os, "getsid", return_value=4100))
        self.real_freeze_tree = bpt._freeze_tree
        self.freeze = stack.enter_context(mock.patch.object(
            bpt, "_freeze_tree", return_value=(self.members, self.members[1:])
        ))
        stack.enter_context(mock.patch.object(bpt, "observe_process", side_effect=self.observe))
        stack.enter_context(mock.patch.object(bpt, "_process_rows", side_effect=lambda: [
            (pid, self.parents.get(pid, 1), item["pgid"])
            for pid, item in self.live.items()
        ]))
        stack.enter_context(mock.patch.object(bpt.os, "kill", side_effect=self.kill))
        self.killpg = stack.enter_context(mock.patch.object(bpt.os, "killpg"))
        stack.enter_context(mock.patch.object(bpt.time, "monotonic", side_effect=lambda: self.now))
        stack.enter_context(mock.patch.object(bpt.time, "sleep", side_effect=self.sleep))

    def observe(self, pid):
        found = self.live.get(pid)
        return dict(found) if found else None

    def kill(self, pid, signum):
        self.delivered.append((pid, signum, self.now))
        self.on_signal(pid, signum)
        if signum == signal.SIGKILL:
            self.live.pop(pid, None)

    def sleep(self, duration):
        self.now += duration
        self.on_sleep()

    def test_term_is_followed_by_continue_and_grace_before_kill(self):
        def change_group(pid, signum):
            if signum == signal.SIGCONT:
                if pid == self.root["pid"]:
                    self.live.pop(pid)  # supervisor exits during grace
                else:
                    self.live[pid]["pgid"] = 4400  # descendants regroup again

        self.on_signal = change_group
        bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertFalse(self.live)
        self.killpg.assert_not_called()  # no unpinned group signal after root exits
        for item in self.members[1:]:
            delivered = [(sig, at) for pid, sig, at in self.delivered if pid == item["pid"]]
            self.assertEqual([sig for sig, _at in delivered],
                             [signal.SIGTERM, signal.SIGCONT, signal.SIGKILL])
            self.assertGreaterEqual(delivered[-1][1], 0.2)

    def test_regroup_between_tree_observation_and_freeze_keeps_children_attributable(self):
        self.live[4400] = identity(4400)  # unrelated sibling, never in the PPID tree
        self.live[4500] = identity(4500, 4100)  # reparented member of the pinned group
        self.freeze.side_effect = self.real_freeze_tree
        drifted = False

        def observe_then_regroup(pid):
            nonlocal drifted
            observed = self.observe(pid)
            if pid == 4200 and not drifted:
                self.live[pid]["pgid"] = 4100
                drifted = True
            return observed

        def rows():
            parents = {4100: 1, 4200: 4100, 4300: 4200, 4400: 1, 4500: 1}
            return [(pid, parents[pid], item["pgid"]) for pid, item in self.live.items()]

        with (
            mock.patch.object(bpt, "observe_process", side_effect=observe_then_regroup),
            mock.patch.object(bpt, "_process_rows", side_effect=rows),
        ):
            bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertTrue(drifted)
        self.assertEqual(list(self.live), [4400])
        self.assertNotIn(4400, [pid for pid, _sig, _at in self.delivered])
        self.killpg.assert_called_once_with(4100, signal.SIGSTOP)

    def test_graceful_tree_exits_after_continue_without_kill(self):
        def exit_after_continue(pid, signum):
            if signum == signal.SIGCONT:
                self.assertIn((pid, signal.SIGTERM, 0.0), self.delivered)
                self.live.pop(pid)

        self.on_signal = exit_after_continue
        bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertFalse(self.live)
        self.assertNotIn(signal.SIGKILL, [sig for _pid, sig, _at in self.delivered])
        self.killpg.assert_not_called()

    def test_reused_pid_during_grace_is_refused_without_killing_replacement(self):
        def replace_worker():
            self.live[4200]["process_start_token"] = "proc:replacement"

        self.on_sleep = replace_worker
        with self.assertRaisesRegex(bpt.ProcessTruthError, "identity changed during cancellation"):
            bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertEqual(self.live[4200]["process_start_token"], "proc:replacement")
        self.assertEqual(set(self.live), {4200})
        self.assertFalse(any(pid == 4200 and sig == signal.SIGKILL
                             for pid, sig, _at in self.delivered))
        self.killpg.assert_not_called()

    def test_reuse_between_last_poll_and_kill_is_rechecked(self):
        real_signal = bpt._signal_identity

        def replace_just_before_kill(recorded, signum, **kwargs):
            if recorded["pid"] == 4200 and signum == signal.SIGKILL:
                self.live[4200]["process_start_token"] = "proc:replacement"
            return real_signal(recorded, signum, **kwargs)

        with mock.patch.object(bpt, "_signal_identity", side_effect=replace_just_before_kill):
            with self.assertRaisesRegex(bpt.ProcessTruthError, "process_start_token"):
                bpt.terminate_attributable_tree(self.root, grace=0)

        self.assertFalse(any(pid == 4200 and sig == signal.SIGKILL
                             for pid, sig, _at in self.delivered))
        self.assertEqual(set(self.live), {4200})
        self.killpg.assert_not_called()

    def test_term_refusal_still_resumes_other_frozen_members(self):
        self.live[4200]["argv_sha256"] = "b" * 64
        with self.assertRaisesRegex(bpt.ProcessTruthError, "argv_sha256"):
            bpt.terminate_attributable_tree(self.root, grace=0)

        for pid in (4100, 4300):
            self.assertIn((pid, signal.SIGCONT, 0.0), self.delivered)
        self.assertNotIn(4200, [pid for pid, _sig, _at in self.delivered])
        self.assertEqual(set(self.live), {4200})
        self.killpg.assert_not_called()

    def test_late_group_orphan_and_escaped_child_are_recensused(self):
        def fork_on_continue(pid, signum):
            if pid == 4200 and signum == signal.SIGCONT:
                self.live[4500] = identity(4500, 4100)  # late group orphan
                self.live[4600] = identity(4600)  # new session, linked by PPID
                self.parents[4600] = 4200
            if pid == 4100 and signum == signal.SIGCONT:
                self.live.pop(pid)

        self.live[4400] = identity(4400)  # unrelated session
        self.on_signal = fork_on_continue
        bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertEqual(set(self.live), {4400})
        for pid in (4500, 4600):
            self.assertTrue(any(target == pid and sig == signal.SIGKILL
                                for target, sig, _at in self.delivered))
        self.assertNotIn(4400, [pid for pid, _sig, _at in self.delivered])
        self.killpg.assert_not_called()

    def test_new_member_during_kill_requires_another_census(self):
        def fork_before_kill(pid, signum):
            if signum == signal.SIGKILL and pid in (4100, 4500):
                child = 4500 if pid == 4100 else 4600
                self.live[child] = identity(child, 4100)

        self.on_signal = fork_before_kill
        bpt.terminate_attributable_tree(self.root, grace=0)

        self.assertFalse(self.live)
        for pid in (4500, 4600):
            self.assertIn((pid, signal.SIGKILL), [(pid, sig) for pid, sig, _at in self.delivered])

    def test_empty_group_is_never_reopened_by_a_later_census(self):
        def exit_root(pid, signum):
            if pid == 4100 and signum == signal.SIGCONT:
                self.live.pop(pid)

        def reuse_empty_group():
            # Both surviving captured members are in group 4200. The original
            # group has disappeared; a later 4100 group cannot authorize us.
            self.live[4500] = identity(4500, 4100)

        self.on_signal = exit_root
        self.on_sleep = reuse_empty_group
        bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertEqual(set(self.live), {4500})
        self.assertNotIn(4500, [pid for pid, _sig, _at in self.delivered])

    def test_exec_during_grace_skips_changed_member_but_kills_peers(self):
        def exec_worker():
            self.live[4200]["argv_sha256"] = "b" * 64

        self.on_sleep = exec_worker
        with self.assertRaisesRegex(bpt.ProcessTruthError, "identity changed during cancellation"):
            bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertEqual(set(self.live), {4200})
        self.assertFalse(any(pid == 4200 and sig == signal.SIGKILL
                             for pid, sig, _at in self.delivered))

    def test_reused_parent_does_not_admit_replacements_children(self):
        def replace_worker():
            self.live[4200]["process_start_token"] = "proc:replacement"
            self.live[4500] = identity(4500)
            self.parents[4500] = 4200

        self.on_sleep = replace_worker
        with self.assertRaises(bpt.ProcessTruthError):
            bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertEqual(set(self.live), {4200, 4500})
        self.assertNotIn(4500, [pid for pid, _sig, _at in self.delivered])

    def test_reused_root_does_not_admit_a_recycled_group(self):
        def replace_root(pid, signum):
            if pid == 4100 and signum == signal.SIGCONT:
                self.live[pid]["process_start_token"] = "proc:replacement"
                self.live[4500] = identity(4500, 4100)

        self.on_signal = replace_root
        with self.assertRaises(bpt.ProcessTruthError):
            bpt.terminate_attributable_tree(self.root, grace=0)

        self.assertEqual(set(self.live), {4100, 4500})
        self.assertNotIn(4500, [pid for pid, _sig, _at in self.delivered])

    def test_persistent_survivor_is_bounded_and_reported(self):
        def ineffective_kill(pid, signum):
            self.delivered.append((pid, signum, self.now))

        with mock.patch.object(bpt.os, "kill", side_effect=ineffective_kill):
            with self.assertRaisesRegex(bpt.ProcessTruthError, "survived cancellation"):
                bpt.terminate_attributable_tree(self.root, grace=0.2)

        self.assertEqual(set(self.live), {4100, 4200, 4300})
        self.assertLess(self.now, 2)

    def test_census_failure_still_kills_known_members_and_reports_failure(self):
        with mock.patch.object(bpt, "_process_rows", side_effect=bpt.ProcessTruthError(
            "process tree observation failed"
        )):
            with self.assertRaisesRegex(bpt.ProcessTruthError, "process tree observation failed"):
                bpt.terminate_attributable_tree(self.root, grace=0)
        self.assertFalse(self.live)

    def test_survivor_prevents_cancelled_receipt_after_peers_are_killed(self):
        def exec_worker(pid, signum):
            if pid == 4200 and signum == signal.SIGCONT:
                self.live[pid]["argv_sha256"] = "b" * 64

        self.on_signal = exec_worker
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "TASK-survivor.d-survivor"
            descriptor = {
                "schema": bpt.DESCRIPTOR_V2,
                "task_id": "TASK-survivor", "attempt_id": "d-survivor", "generation": 1,
                "created_at": bpt.utc_now(), **self.root,
                **{key + "_path": str(base) + suffix for key, suffix in (
                    ("context", ".context.json"), ("log", ".log"),
                    ("receipt", ".receipt.json"),
                )},
            }
            dispatch = Path(str(base) + ".dispatch.json")
            dispatch.write_text(json.dumps(descriptor))
            Path(descriptor["context_path"]).write_text(json.dumps({"authority": {
                key: descriptor[key] for key in bpt.ATTEMPT_IDENTITY_FIELDS
            }}))
            with mock.patch.object(bpt, "process_run_state", return_value="running"):
                with self.assertRaisesRegex(bpt.ProcessTruthError, "survived cancellation"):
                    bpt.cancel_attempt(dispatch, grace=0)
            self.assertEqual(set(self.live), {4200})
            self.assertFalse(Path(descriptor["receipt_path"]).exists())


if __name__ == "__main__":
    unittest.main()
