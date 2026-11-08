import unittest

from pbdata.collect.parse import Doc, Segment
from pbdata.profiles import pitchboard as prof


class SimultaneousPolicyTests(unittest.TestCase):
    def policy(self, text):
        return [e for e in prof.extract(Doc('html', segments=[Segment(0, 'p', text)]))
                if e.field == 'rights.simultaneous']

    def test_parenthetical_definition(self):
        for declaration, expected in [('are permitted', 'allowed'),
                                      ('are not accepted', 'not_allowed'),
                                      ("aren't allowed", 'not_allowed')]:
            with self.subTest(declaration=declaration):
                text = ('Simultaneous submissions (submissions which have been sent to other '
                        'literary magazines) ' + declaration + '.')
                result = self.policy(text)
                self.assertEqual([e.value for e in result], [{'state': expected}])
                self.assertEqual(self.policy(result[0].excerpt)[0].value, result[0].value)

    def test_no_permission_from_conditions_or_other_subjects(self):
        for text in [
            'Simultaneous submissions: please withdraw if accepted elsewhere.',
            'Simultaneous submissions are not accepted.',
            'Simultaneous submissions may not be sent. Previously accepted work is welcome.',
            'Simultaneous submissions (not permitted) are discussed in our FAQ.',
            'Simultaneous submissions are discouraged; poetry is welcome.',
        ]:
            with self.subTest(text=text):
                self.assertFalse(any(e.value == {'state': 'allowed'} for e in self.policy(text)))

    def test_existing_direct_declarations(self):
        for text, state in [('Simultaneous submissions are welcome.', 'allowed'),
                            ('We accept simultaneous submissions.', 'allowed'),
                            ('No simultaneous submissions.', 'not_allowed'),
                            ('동시 투고 가능', 'allowed'), ('중복 투고 불가', 'not_allowed')]:
            self.assertEqual([e.value for e in self.policy(text)], [{'state': state}])
