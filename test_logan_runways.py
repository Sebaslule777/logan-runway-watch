import io
import json
import os
from contextlib import redirect_stdout
import tempfile
import unittest
from unittest import mock

import logan_runways as lr


class ParseAtis(unittest.TestCase):
    def check(self, text, landing, departing, default=None):
        arr, dep = lr.parse_atis(text, default)
        self.assertEqual(arr, set(landing), "landing")
        self.assertEqual(dep, set(departing), "departing")

    def test_southwest_flow(self):
        self.check("BOS ATIS INFO Y 1854Z. 22012KT 10SM FEW250 13/M04 A2995 (TWO NINER NINER FIVE). "
                   "ILS RWY 22L APCH IN USE. VISUAL APCH RWY 27. DEPG RWYS 22R, 22L. "
                   "NOTAMS... TWY E CLSD. READBACK ALL RUNWAY HOLD SHORT INSTRUCTIONS. ADVS YOU HAVE INFO Y.",
                   ["22L", "27"], ["22R", "22L"])

    def test_northeast_flow_one_clause(self):
        self.check("BOS ATIS INFO K 0954Z. 04015G22KT 6SM -RA BR OVC012 08/07 A2988. "
                   "LANDING RWYS 4R, 4L, DEPARTING RWY 4R, 9. RWY 15R/33L CLSD.",
                   ["4R", "4L"], ["4R", "9"])

    def test_leading_zero_and_and(self):
        self.check("SIMUL APCHS IN USE RWYS 04R AND 04L. DEPTG RWY 09.", ["4R", "4L"], ["9"])

    def test_keyword_after_runway(self):
        self.check("RWY 33L ILS APCH IN USE. RWY 27 RNAV DEPS.", ["33L"], ["27"])

    def test_nearest_keyword_wins(self):
        self.check("DEPG RWY 9, ILS RWY 4R APCH", ["4R"], ["9"])

    def test_closures_and_hold_short_ignored(self):
        self.check("LAND AND HOLD SHORT OPERATIONS IN EFFECT RWY 4R HOLD SHORT OF RWY 9. "
                   "RWY 14/32 CLSD. RWY 4R RVR 2400. LDG RWY 4L.", ["4L"], [])

    def test_weather_numbers_not_runways(self):
        self.check("BOS ATIS INFO A 0054Z. 33010KT 10SM CLR 13/M04 A3012. LDG RWY 33L.", ["33L"], [])

    def test_arrival_only_atis_default(self):
        self.check("BOS ARR INFO B 1154Z. 27008KT 10SM SCT050. RWY 27, RWY 22L IN USE.",
                   ["27", "22L"], [], default="arr")

    def test_departure_only_atis_default(self):
        self.check("BOS DEP INFO C 1154Z. 27008KT 10SM. RWYS 22R AND 27 IN USE.", [], ["22R", "27"], default="dep")


class Wind(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(lr.parse_wind("X. 04015G22KT 6SM"), (40, 15, 22))
        self.assertEqual(lr.parse_wind("VRB03KT"), (None, 3, None))
        self.assertIsNone(lr.parse_wind("no wind here"))

    def test_components(self):
        head, cross = lr.wind_components(270, 10, "27")
        self.assertAlmostEqual(head, 10)
        self.assertAlmostEqual(cross, 0, places=6)
        head, _ = lr.wind_components(270, 10, "9")
        self.assertAlmostEqual(head, -10)


class Watch(unittest.TestCase):
    def test_parse_watch(self):
        self.assertEqual(lr.parse_watch(["arr:4R", "dep:27", "33l"]),
                         {("arr", "4R"), ("dep", "27"), ("arr", "33L"), ("dep", "33L")})

    def test_bad_runway(self):
        with self.assertRaises(SystemExit):
            lr.parse_watch(["arr:5Z"])

    def test_alert_on_change(self):
        responses = [
            [{"airport": "KBOS", "type": "combined", "code": "K",
              "datis": "BOS ATIS INFO K. 04012KT. LDG RWYS 4R, 4L. DEPG RWY 9."}],
            [{"airport": "KBOS", "type": "combined", "code": "L",
              "datis": "BOS ATIS INFO L. 27012KT. LDG RWY 27. DEPG RWYS 22R, 22L."}],
            [{"airport": "KBOS", "type": "combined", "code": "M",
              "datis": "BOS ATIS INFO M. 27012KT. LDG RWY 27. DEPG RWYS 22R, 22L."}],
        ]
        with tempfile.TemporaryDirectory() as d:
            args = lr.argparse.Namespace(log=os.path.join(d, "log.csv"), verbose=False,
                                         ntfy=None, no_desktop=True)
            watched = lr.parse_watch(["arr:27"])
            state, notes = {}, []
            with mock.patch.object(lr, "notify", lambda title, *a, **k: notes.append(title)):
                for resp in responses:
                    with mock.patch.object(lr, "http_get", return_value=json.dumps(resp)):
                        state = lr.check_once(args, watched, state)
            self.assertEqual(notes, ["Planes over your house"])
            with open(args.log) as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[0].split(",")[:4], ["time_utc", "atis", "landing", "departing"])
            self.assertEqual(len(lines), 4)
            self.assertIn(",K,4L 4R,9,", lines[1])


class Report(unittest.TestCase):
    def make_rows(self, spec):
        """spec: list of (minutes after a fixed local midnight, flag)."""
        base = lr.datetime(2026, 10, 15, 0, 0).astimezone()
        return [{"time": base + lr.timedelta(minutes=m), "watched_active": f,
                 "landing": "27", "departing": "33L 27" if f == "1" else "22R"} for m, f in spec]

    def test_stretches_and_gaps(self):
        rows = self.make_rows([(0, "0"), (5, "1"), (10, "1"), (15, "0"),   # 5:00-15:00 -> 10 min
                               (20, "1"), (25, "1"), (200, "1"),           # gap: ends at :25, new one at 200
                               (205, "1")])                                # still going at end of log
        s = lr.noisy_stretches(rows)
        self.assertEqual(len(s), 3)
        self.assertEqual((s[0]["end"] - s[0]["start"]).seconds, 600)
        self.assertEqual((s[1]["end"] - s[1]["start"]).seconds, 300)
        self.assertTrue(s[2].get("ongoing"))
        self.assertEqual(s[0]["setups"], ["land 27 / depart 33L 27"])

    def test_night_minutes(self):
        base = lr.datetime(2026, 10, 15, 21, 0).astimezone()
        self.assertEqual(lr.night_minutes(base, base + lr.timedelta(hours=2)), 60)   # 21:00-23:00
        self.assertEqual(lr.night_minutes(base.replace(hour=12), base.replace(hour=13)), 0)

    def test_report_runs_on_log_file(self):
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log.csv")
            now = lr.datetime.now(lr.timezone.utc).replace(second=0, microsecond=0)
            with open(log, "w", newline="") as f:
                w = lr.csv.writer(f)
                w.writerow(lr.LOG_FIELDS)
                for i, flag in enumerate(["0", "1", "1", "1", "0"]):
                    t = now - lr.timedelta(minutes=5 * (5 - i))
                    w.writerow([t.strftime("%Y-%m-%dT%H:%MZ"), "K", "27", "33L", 310, 12, "", flag])
            out = io.StringIO()
            with redirect_stdout(out):
                lr.main(["--log", log, "report"])
            text = out.getvalue()
            self.assertIn("15 min", text)
            self.assertIn("land 27 / depart 33L", text)
            self.assertIn("617", text)


if __name__ == "__main__":
    unittest.main()
