import unittest

import run


def outcomes(seconds):
    return [{'id': f'test_{index}', 'seconds': value} for index, value in enumerate(seconds)]


class SuitePaceTest(unittest.TestCase):
    recorded = {'test_0': 40.0, 'test_1': 30.0}

    def test_a_run_near_the_record_states_its_pace_without_an_alarm(self):
        note = run.pace(outcomes([44.0, 33.0]), self.recorded)
        self.assertIn('1.10 times the recorded seconds', note)
        self.assertNotIn('SLOW', note)

    def test_a_run_several_times_the_record_is_reported_slow(self):
        note = run.pace(outcomes([160.0, 120.0]), self.recorded)
        self.assertIn('SLOW: 4.00 times the recorded seconds of 2 tests', note)

    def test_tests_without_a_record_do_not_count(self):
        note = run.pace(outcomes([44.0, 33.0, 900.0]), self.recorded)
        self.assertIn('1.10 times', note)
        self.assertNotIn('SLOW', note)

    def test_a_record_too_short_to_compare_stays_silent(self):
        self.assertEqual(run.pace(outcomes([400.0]), {'test_0': 40.0}), '')
        self.assertEqual(run.pace(outcomes([400.0]), {}), '')


if __name__ == '__main__':
    unittest.main()
