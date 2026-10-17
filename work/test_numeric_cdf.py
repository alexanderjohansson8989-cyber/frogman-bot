import copy
import json
import unittest
from pathlib import Path
from numeric_cdf import validate_numeric_cdf


class Tests(unittest.TestCase):
    def setUp(self):
        self.fields = json.loads(Path("work/pilot_question.json").read_text(encoding="utf-8"))["fields"]
        self.cdf = [0.001 + 0.998 * i / 200 for i in range(201)]

    def test_open_tails_and_grid(self):
        self.assertEqual(len(validate_numeric_cdf(self.cdf, self.fields)), 201)
        self.assertEqual(self.cdf[0], 0.001)
        self.assertEqual(self.cdf[-1], 0.999)

    def test_invalid_values_shape_and_tail(self):
        for bad in (self.cdf[:-1], [float("nan")] + self.cdf[1:],
                    [True] + self.cdf[1:], [0] + self.cdf[1:], self.cdf[:-1] + [1]):
            with self.assertRaises(ValueError): validate_numeric_cdf(bad, self.fields)

    def test_plateau_reverse_and_concentration(self):
        for a, b in ((0.4,0.4), (0.4,0.3), (0.3,0.6)):
            cdf = self.cdf.copy(); cdf[99:101] = [a,b]
            with self.assertRaises(ValueError): validate_numeric_cdf(cdf, self.fields)

    def test_closed_tails(self):
        fields = copy.deepcopy(self.fields)
        fields.update(open_lower_bound=False, open_upper_bound=False)
        self.assertEqual(validate_numeric_cdf([i/200 for i in range(201)], fields)[-1], 1)
        with self.assertRaises(ValueError): validate_numeric_cdf(self.cdf, fields)

    def test_other_count_and_invalid_grid(self):
        fields = {"inbound_outcome_count":10, "open_lower_bound":True, "open_upper_bound":True,
                  "scaling":{"continuous_range":list(range(11))}}
        self.assertEqual(len(validate_numeric_cdf([0.001+0.998*i/10 for i in range(11)], fields)),11)
        fields["scaling"]["continuous_range"][5] = 0
        with self.assertRaises(ValueError): validate_numeric_cdf([0.001+0.998*i/10 for i in range(11)], fields)

    def test_synthetic_empirical_shape_is_not_submission(self):
        data = json.loads(Path("outputs/approval_underlag.json").read_text(encoding="utf-8"))
        for row in data["benchmarks"]:
            with self.assertRaises(ValueError): validate_numeric_cdf(row["cdf_leq"], self.fields)


if __name__ == "__main__": unittest.main()
