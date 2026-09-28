import unittest

from core.intro_builder import MIN_FACT_DELTA, should_rebuild_introduction


def _inputs(*, identity=None, fact_updates=0, value="", source=""):
    return {
        "meta": {"source": source, "updated_at": 0, "value": value},
        "identity": identity or {},
        "clusters": [],
        "fact_delta": [],
        "identity_updates": 0,
        "fact_updates": fact_updates,
    }


class IntroBuilderGateTests(unittest.TestCase):
    def test_name_alone_does_not_build_an_introduction(self):
        self.assertFalse(should_rebuild_introduction(_inputs(identity={"name": "Ada Lovelace"})))

    def test_enough_facts_build_an_introduction(self):
        self.assertTrue(should_rebuild_introduction(_inputs(
            identity={"name": "Ada Lovelace"},
            fact_updates=MIN_FACT_DELTA,
        )))

    def test_user_authored_introduction_is_left_alone(self):
        self.assertFalse(should_rebuild_introduction(_inputs(
            identity={"name": "Ada Lovelace"},
            fact_updates=MIN_FACT_DELTA,
            value="I wrote this.",
            source="user",
        )))


if __name__ == "__main__":
    unittest.main()
