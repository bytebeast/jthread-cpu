"""Tests for jthread-cpu.  Standard library only:  python3 -m unittest discover -s tests

If `jsonschema` happens to be installed, JSON output is also validated against
the published schema; otherwise those checks are skipped.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "jthread-cpu")
EXAMPLES = os.path.join(ROOT, "examples")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
SCHEMA_FILE = os.path.join(ROOT, "schema", "jthread-cpu-v1.schema.json")


def load_module():
    # the script has no .py suffix, so load it explicitly
    loader = importlib.machinery.SourceFileLoader("jthread_cpu", SCRIPT)
    spec = importlib.util.spec_from_loader("jthread_cpu", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


jc = load_module()


def run_main(*argv):
    """Run main() and return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = jc.main(list(argv))
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


def example_json(*extra):
    rc, out, _ = run_main(
        "--jstack-file",
        os.path.join(EXAMPLES, "dump.txt"),
        "--top-file",
        os.path.join(EXAMPLES, "top.txt"),
        "--cpus",
        "8",
        "-o",
        "json",
        *extra,
    )
    assert rc == 0, rc
    return json.loads(out)


try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None


class ParseTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(EXAMPLES, "dump.txt")) as fh:
            self.threads, self.meta = jc.parse_jstack(fh.read())

    def test_example_dump(self):
        self.assertEqual(len(self.threads), 18)
        self.assertEqual(self.meta["pid"], 48213)
        by = {t.name: t for t in self.threads}
        w3 = by["order-worker-3"]
        self.assertEqual((w3.nid, w3.nid_hex, w3.state), (48290, "0xbca2", "RUNNABLE"))
        self.assertEqual(
            w3.app_frame(),
            "com.acme.shop.pricing.CouponValidator.isValid(CouponValidator.java:88)",
        )
        self.assertTrue(by["GC Thread#0"].is_jvm_internal)
        self.assertIn("kafka-producer-network-thread | shop-producer", by)

    def test_jdk21_decimal_nid_and_bracket_tid(self):
        text = (
            '"main" #1 [556] prio=5 os_prio=0 cpu=65.23ms elapsed=15.45s tid=0x00007f714801aee0 '
            "nid=556 in Object.wait()  [0x00007f714d7fe000]\n"
            "   java.lang.Thread.State: WAITING (on object monitor)\n"
        )
        (t,), _ = jc.parse_jstack(text)
        self.assertEqual(
            (t.nid, t.nid_hex, t.nid_raw, t.jnum), (556, "0x22c", "556", 1)
        )

    def test_real_jdk21_fixture(self):
        with open(os.path.join(FIXTURES, "jdk21-dump.txt")) as fh:
            threads, meta = jc.parse_jstack(fh.read())
        self.assertGreater(len(threads), 20)
        self.assertTrue(all(t.nid is not None for t in threads))
        self.assertIn("21.0", meta["jvm_version"])

    def test_real_dumps_from_each_jdk(self):
        # captured with jcmd Thread.print -l from the same test program on JDK 8, 11, 21, 25
        for jdk, hex_nid, has_cpu in (
            ("8", True, False),
            ("11", True, True),
            ("21", False, True),
            ("25", False, True),
        ):
            with open(os.path.join(FIXTURES, "jdk%s-dump.txt" % jdk)) as fh:
                threads, _ = jc.parse_jstack(fh.read())
            by = {t.name: t for t in threads}
            w = by["order-worker-0"]
            self.assertEqual(w.state, "RUNNABLE", jdk)
            self.assertIsNotNone(w.nid, jdk)
            self.assertEqual(w.nid_raw.startswith("0x"), hex_nid, jdk)
            self.assertEqual(w.cpu_ms is not None, has_cpu, jdk)
            self.assertTrue(w.top_frame().startswith("java.util.regex."), jdk)
            locks = jc.contended_locks(jc.merge(threads, {})[0])
            self.assertEqual(locks[0]["owner"], "lock-holder-1", jdk)
            self.assertEqual(
                locks[0]["waiters"], ["blocked-0", "blocked-1", "blocked-2"], jdk
            )

    def test_jdk8_dump_alone_refuses_all_zero_report(self):
        rc, _, err = run_main("--jstack-file", os.path.join(FIXTURES, "jdk8-dump.txt"))
        self.assertEqual(rc, 1)
        self.assertIn("no per-thread CPU data", err)

    def test_ownable_synchronizers_become_locks_held(self):
        text = (
            '"w" #9 prio=5 os_prio=0 tid=0x1 nid=0x10 runnable  [0x0]\n'
            "   java.lang.Thread.State: RUNNABLE\n"
            "\tat a.B.c(B.java:1)\n\n"
            "   Locked ownable synchronizers:\n"
            "\t- <0x00000000deadbeef> (a java.util.concurrent.locks.ReentrantLock$NonfairSync)\n"
        )
        (t,), _ = jc.parse_jstack(text)
        self.assertEqual(
            t.locks_held,
            [
                "<0x00000000deadbeef> (a java.util.concurrent.locks.ReentrantLock$NonfairSync)"
            ],
        )
        self.assertEqual(t.frames, ["at a.B.c(B.java:1)"])

    def test_top_file_and_window(self):
        with open(os.path.join(EXAMPLES, "top.txt")) as fh:
            cpu, window, iters = jc.parse_top_file(fh.read())
        self.assertEqual((window, iters), (2.0, 2))
        self.assertEqual(cpu[48290]["cpu"], 97.3)  # the last iteration wins

    def test_top_window_across_midnight(self):
        blocks = ["top - 23:59:59 up\nPID %CPU\n", "top - 00:00:04 up\nPID %CPU\n"]
        self.assertEqual(jc.top_window(blocks), 5.0)
        self.assertIsNone(jc.top_window(blocks[:1]))


class NormalisationTests(unittest.TestCase):
    def test_example_totals(self):
        d = example_json()
        self.assertEqual(d["cpu"]["total_pct"], 221.7)
        self.assertEqual(d["cpu"]["total_cores"], 2.217)
        self.assertEqual(d["cpu"]["pct_of_capacity"], 27.71)
        self.assertEqual(d["capacity"]["capacity_cores"], 8.0)
        self.assertEqual(d["capacity"]["source"], "--cpus")
        self.assertEqual(d["sample_window_s"], 2.0)
        top = d["threads"][0]
        self.assertEqual((top["name"], top["cpu_pct"]), ("order-worker-3", 97.3))
        self.assertAlmostEqual(top["share_of_jvm_pct"], 43.89, places=2)
        self.assertAlmostEqual(top["cpu_pct_of_capacity"], 12.16, places=2)

    def test_offline_without_cpus_leaves_capacity_unknown(self):
        rc, out, _ = run_main(
            "--jstack-file",
            os.path.join(EXAMPLES, "dump.txt"),
            "--top-file",
            os.path.join(EXAMPLES, "top.txt"),
            "-o",
            "json",
        )
        d = json.loads(out)
        self.assertIsNone(d["capacity"]["capacity_cores"])
        self.assertIsNone(d["cpu"]["pct_of_capacity"])

    def test_capacity_override_and_host(self):
        self.assertEqual(jc.cpu_capacity(override=3)["capacity"], 3.0)
        info = jc.cpu_capacity(os.getpid())
        self.assertGreaterEqual(info["capacity"], 0.01)
        self.assertLessEqual(info["capacity"], info["host"])

    def _fake_cgroup(self, files, proc_cgroup):
        import tempfile

        tmp = tempfile.mkdtemp()
        cg, proc = os.path.join(tmp, "cgroup"), os.path.join(tmp, "proc")
        for rel, content in files.items():
            path = os.path.join(cg, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(content + "\n")
        os.makedirs(os.path.join(proc, "77"))
        with open(os.path.join(proc, "77", "cgroup"), "w") as fh:
            fh.write(proc_cgroup)
        old = (jc.CGROUP_ROOT, jc.PROC_ROOT)
        jc.CGROUP_ROOT, jc.PROC_ROOT = cg, proc
        self.addCleanup(
            lambda: (
                setattr(jc, "CGROUP_ROOT", old[0]) or setattr(jc, "PROC_ROOT", old[1])
            )
        )

    def test_cgroup_v2_tightest_ancestor_wins(self):
        self._fake_cgroup(
            {
                "cpu.max": "max 100000",
                "kubepods/cpu.max": "max 100000",
                "kubepods/pod1/cpu.max": "400000 100000",
                "kubepods/pod1/ctr/cpu.max": "150000 100000",
            },
            "0::/kubepods/pod1/ctr\n",
        )
        self.assertEqual(jc._cgroup_quota_cores(77), 1.5)

    def test_cgroup_v2_unlimited(self):
        self._fake_cgroup(
            {"cpu.max": "max 100000", "a/cpu.max": "max 100000"}, "0::/a\n"
        )
        self.assertIsNone(jc._cgroup_quota_cores(77))

    def test_cgroup_v1(self):
        self._fake_cgroup(
            {
                "cpu,cpuacct/cpu.cfs_quota_us": "-1",
                "cpu,cpuacct/cpu.cfs_period_us": "100000",
                "cpu,cpuacct/docker/abc/cpu.cfs_quota_us": "200000",
                "cpu,cpuacct/docker/abc/cpu.cfs_period_us": "100000",
            },
            "4:cpu,cpuacct:/docker/abc\n3:memory:/docker/abc\n",
        )
        self.assertEqual(jc._cgroup_quota_cores(77), 2.0)

    def test_cgroup_other_namespace_falls_back_to_own(self):
        # the JVM's path does not exist under our mount (it is in another namespace)
        self._fake_cgroup({"cpu.max": "50000 100000"}, "0::/somewhere/else\n")
        self.assertEqual(jc._cgroup_quota_cores(77), 0.5)

    def test_bar_is_one_core(self):
        self.assertEqual(jc.bar(100.0, 10), jc.G.BAR * 10)
        self.assertEqual(jc.bar(250.0, 10), jc.G.BAR * 10)
        self.assertEqual(jc.bar(0.0, 4), jc.G.EMPTY * 4)


class HintTests(unittest.TestCase):
    def test_example_hints_are_deterministic(self):
        a = example_json()["hints"]
        b = example_json()["hints"]
        self.assertEqual(a, b)
        self.assertEqual(
            [h["id"] for h in a],
            ["LOCK_CONTENTION", "REGEX_BACKTRACKING", "SELECTOR_SPIN"],
        )
        lock = a[0]
        self.assertIn("order-worker-4", lock["threads"])
        self.assertTrue(
            any(
                "StockLedger.reserve(StockLedger.java:81)" in e
                for e in lock["evidence"]
            )
        )
        regex = a[1]
        self.assertTrue(any("CouponValidator.java:88" in e for e in regex["evidence"]))

    def test_all_hint_ids_are_declared(self):
        with open(SCRIPT) as fh:
            src = fh.read()
        import re

        used = set(re.findall(r'_hint\(\s*"([A-Z_]+)"', src))
        self.assertTrue(used)
        self.assertEqual(used - set(jc.HINT_RULES), set())

    def test_deadlock_is_critical_and_first(self):
        text = (
            '"a" #1 prio=5 os_prio=0 tid=0x1 nid=0x10 waiting for monitor entry  [0x0]\n'
            "   java.lang.Thread.State: BLOCKED (on object monitor)\n"
            "\tat x.Y.f(Y.java:1)\n"
            "\t- waiting to lock <0x00000000000000b0> (a java.lang.Object)\n"
            "\t- locked <0x00000000000000a0> (a java.lang.Object)\n\n"
            '"b" #2 prio=5 os_prio=0 tid=0x2 nid=0x11 waiting for monitor entry  [0x0]\n'
            "   java.lang.Thread.State: BLOCKED (on object monitor)\n"
            "\tat x.Y.g(Y.java:2)\n"
            "\t- waiting to lock <0x00000000000000a0> (a java.lang.Object)\n"
            "\t- locked <0x00000000000000b0> (a java.lang.Object)\n\n"
            "Found one Java-level deadlock:\n=============================\n"
            '"a":\n  waiting to lock monitor 0x1 (object 0x00000000000000b0, a java.lang.Object),\n'
            '  which is held by "b"\n'
            '"b":\n  waiting to lock monitor 0x2 (object 0x00000000000000a0, a java.lang.Object),\n'
            '  which is held by "a"\n\n'
            "Java stack information for the threads listed above:\n"
            "===================================================\n"
            "Found 1 deadlock.\n"
        )
        threads, meta = jc.parse_jstack(text)
        self.assertEqual(len(threads), 2)
        rows, orphans = jc.merge(
            threads,
            {
                0x10: {"cpu": 0.0, "comm": "a", "time": "", "state": "", "mem_pct": ""},
                0x11: {"cpu": 0.0, "comm": "b", "time": "", "state": "", "mem_pct": ""},
            },
        )
        ctx = {
            "pid": 1,
            "capacity": {"capacity": 4.0},
            "window": 2.0,
            "method_id": "top",
        }
        an = jc.analyse(rows, orphans, meta, ctx)
        hints = jc.investigation_hints(rows, orphans, meta, ctx, an)
        self.assertEqual(hints[0]["id"], "DEADLOCK")
        self.assertEqual(hints[0]["severity"], "critical")
        self.assertEqual(hints[0]["threads"], ["a", "b"])

    def test_object_wait_is_not_lock_ownership(self):
        # Object.wait() prints "- locked" for the monitor it released
        with open(os.path.join(FIXTURES, "jdk21-dump.txt")) as fh:
            threads, _ = jc.parse_jstack(fh.read())
        rows, _ = jc.merge(threads, {})
        owners = {l["lock"]: l["owner"] for l in jc.contended_locks(rows)}
        self.assertNotIn("main", owners.values())


class RepeatedSamplingTests(unittest.TestCase):
    def _thread(self, name, nid, frame):
        t = jc.JThread()
        t.name, t.nid, t.state = name, nid, "RUNNABLE"
        t.frames = ["at " + frame]
        return t

    def test_tracker_classifies_and_flags_loops(self):
        tr = jc.Tracker(25.0)
        loop = self._thread("spinner", 10, "com.x.Loop.run(Loop.java:5)")
        burst = self._thread("bursty", 11, "com.x.Job.run(Job.java:9)")
        for cpu_b in (0.0, 95.0, 0.0, 0.0):
            rows = [
                {"thread": loop, "cpu": 99.0, "sample": None},
                {"thread": burst, "cpu": cpu_b, "sample": None},
            ]
            tr.add(rows, {}, {"total": 99.0 + cpu_b}, [], 42)
        s = tr.summary()
        self.assertEqual(s["rounds"], 4)
        by = {t["name"]: t for t in s["threads"]}
        self.assertEqual(by["spinner"]["classification"], "persistent")
        self.assertEqual(by["spinner"]["rounds_hot"], 4)
        self.assertEqual(by["bursty"]["classification"], "spike")
        self.assertEqual(by["bursty"]["series"], [0.0, 95.0, 0.0, 0.0])
        self.assertEqual(
            [h["id"] for h in s["hints"]], ["PERSISTENT_LOOP", "BURSTY_THREAD"]
        )

    def test_threads_appearing_later_are_padded(self):
        tr = jc.Tracker(25.0)
        a = self._thread("a", 1, "x.A.a(A.java:1)")
        b = self._thread("b", 2, "x.B.b(B.java:1)")
        tr.add([{"thread": a, "cpu": 50.0, "sample": None}], {}, {"total": 50.0}, [], 1)
        tr.add(
            [
                {"thread": a, "cpu": 50.0, "sample": None},
                {"thread": b, "cpu": 60.0, "sample": None},
            ],
            {},
            {"total": 110.0},
            [],
            1,
        )
        by = {t["name"]: t for t in tr.summary()["threads"]}
        self.assertEqual(by["b"]["series"], [None, 60.0])
        self.assertEqual(by["b"]["avg_cpu_pct"], 30.0)

    def test_offline_ignores_count(self):
        rc, out, err = run_main(
            "--jstack-file",
            os.path.join(EXAMPLES, "dump.txt"),
            "--top-file",
            os.path.join(EXAMPLES, "top.txt"),
            "-c",
            "3",
            "-o",
            "json",
        )
        self.assertEqual(rc, 0)
        self.assertIn("--count/--watch need a live JVM", err)
        self.assertEqual(json.loads(out)["kind"], "report")


class SchemaTests(unittest.TestCase):
    def test_repo_schema_matches_embedded(self):
        with open(SCHEMA_FILE) as fh:
            self.assertEqual(json.load(fh), json.loads(json.dumps(jc.json_schema())))

    def test_versions_line_up(self):
        s = jc.json_schema()
        self.assertEqual(s["x-schema-version"], jc.SCHEMA_VERSION)
        self.assertTrue(
            s["$id"].endswith("-v%s.schema.json" % jc.SCHEMA_VERSION.split(".")[0])
        )
        self.assertEqual(example_json()["schema_version"], jc.SCHEMA_VERSION)

    def test_legacy_fields_still_present(self):
        d = example_json()
        for key in (
            "generated_at",
            "pid",
            "cmdline",
            "method",
            "sample_window_s",
            "cpu_count",
            "ppid",
            "process",
            "deadlocks",
            "deadlock_stacks",
            "total_thread_cpu_pct",
            "threads",
            "unmatched_native_threads",
        ):
            self.assertIn(key, d)
        for key in (
            "name",
            "os_tid",
            "nid",
            "nid_hex",
            "state",
            "cpu_pct",
            "top_frame",
            "stack",
            "locks_held",
            "waiting_on",
        ):
            self.assertIn(key, d["threads"][0])

    @unittest.skipIf(jsonschema is None, "jsonschema not installed")
    def test_outputs_validate(self):
        schema = jc.json_schema()
        jsonschema.Draft202012Validator.check_schema(schema)
        v = jsonschema.Draft202012Validator(schema)
        docs = [example_json(), example_json("--no-hints")]
        rc, out, _ = run_main(
            "--jstack-file", os.path.join(EXAMPLES, "dump.txt"), "-o", "json"
        )
        docs.append(json.loads(out))
        rc, out, _ = run_main(
            "--jstack-file",
            os.path.join(FIXTURES, "jdk21-dump.txt"),
            "--top-file",
            os.path.join(FIXTURES, "jdk21-top.txt"),
            "-o",
            "jsonl",
        )
        docs.append(json.loads(out))
        tr = jc.Tracker(25.0)
        tr.add([], {5: {"cpu": 80.0, "comm": "x"}}, {"total": 80.0}, [], None)
        docs.append(jc.build_series([docs[0]], tr.summary()))
        for d in docs:
            errors = sorted(v.iter_errors(d), key=str)
            self.assertEqual(errors, [], [e.message for e in errors[:3]])


class TextOutputTests(unittest.TestCase):
    def test_text_report_sections(self):
        rc, out, _ = run_main(
            "--jstack-file",
            os.path.join(EXAMPLES, "dump.txt"),
            "--top-file",
            os.path.join(EXAMPLES, "top.txt"),
            "--cpus",
            "8",
            "--no-color",
        )
        self.assertEqual(rc, 0)
        for s in (
            "HOTTEST THREADS",
            "NOT IN THREAD DUMP",
            "CPU BY THREAD GROUP",
            "THREAD STATES",
            "BLOCKED THREADS",
            "STACKS OF THE HOTTEST THREADS",
            "NEXT STEPS",
            "221.7% = 2.22 cores",
            "27.7% of capacity",
            "window: 2.0s",
        ):
            self.assertIn(s, out)
        self.assertNotIn("\033[", out)

    def test_print_schema_and_version(self):
        rc, out, _ = run_main("--print-schema")
        self.assertEqual(json.loads(out)["$id"], jc.SCHEMA_ID)
        rc, out, _ = run_main("--version")
        self.assertEqual(out.strip(), "jthread-cpu %s" % jc.__version__)


if __name__ == "__main__":
    unittest.main()
