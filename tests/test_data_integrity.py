import json
import os
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from pipeline.build import _cached_bars, resolve_session

SHANGHAI = ZoneInfo('Asia/Shanghai')
DAY = date(2026, 11, 9)

class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 9, 16, 10, tzinfo=tz)

class DataIntegrityTests(unittest.TestCase):
    def test_intraday_kline_cache_is_not_reused_after_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '000001.json'
            intraday = [{'date': DAY.isoformat(), 'close': 1}] * 60
            path.write_text(json.dumps({'as_of': DAY.isoformat(), 'bars': intraday}))
            timestamp = datetime(2026, 11, 9, 14, 0, tzinfo=SHANGHAI).timestamp()
            os.utime(path, (timestamp, timestamp))
            close = [{'date': DAY.isoformat(), 'close': 2}] * 60
            with patch('pipeline.build.CACHE_DIR', Path(directory)), patch('pipeline.build.datetime', FixedDateTime), patch('pipeline.build.fetch_kline', return_value=close) as fetch:
                self.assertEqual(_cached_bars('000001', DAY, False)[-1]['close'], 2)
                fetch.assert_called_once()

    def test_outdated_kline_is_not_scored(self):
        stale = [{'date': '2026-11-06', 'close': 1}] * 60
        with tempfile.TemporaryDirectory() as directory, patch('pipeline.build.CACHE_DIR', Path(directory)), patch('pipeline.build.datetime', FixedDateTime), patch('pipeline.build.fetch_kline', return_value=stale):
            self.assertEqual(_cached_bars('000001', DAY, False), [])

    def test_expired_calendar_and_before_open_do_not_publish(self):
        self.assertEqual(resolve_session(DAY, time(16), [date(2026, 11, 6)], True)[0], 'skip')
        self.assertEqual(resolve_session(DAY, time(9), [DAY], True)[0], 'skip')

    def test_noon_is_intraday_and_afternoon_is_close(self):
        self.assertEqual(resolve_session(DAY, time(12, 30), [DAY], False)[2], 'intraday')
        self.assertEqual(resolve_session(DAY, time(15, 30), [DAY], False)[2], 'close')

if __name__ == '__main__':
    unittest.main()
