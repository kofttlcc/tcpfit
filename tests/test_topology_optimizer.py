import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "orchestrator"))
from topology_optimizer import HostConfig, Path as Route, Peer, Policy, Sample, Trial, choose, validate_topology


CFG = HostConfig("cubic", "fq_codel", 4 << 20, 1000)


def trial(name, values, retrans=10, packets=10000, valid=True):
    cfg = CFG if name == "base" else HostConfig(name, "fq", 8 << 20, 5000)
    return Trial(cfg, [Sample(path, "forward", value, retrans, packets) for path, value in values.items()], "test", valid)


class TopologyTests(unittest.TestCase):
    def test_single_peer_is_supported(self):
        validate_topology([Peer("p", "127.0.0.1", "upstream")], [], "terminator")

    def test_two_peers_require_roles(self):
        with self.assertRaisesRegex(ValueError, "明確指定"):
            validate_topology([Peer("a", "a", "unknown"), Peer("b", "b", "downstream")], [], "proxy")

    def test_more_than_two_requires_pairing(self):
        peers = [Peer("u", "u", "upstream"), Peer("d1", "d1", "downstream"), Peer("d2", "d2", "downstream")]
        with self.assertRaisesRegex(ValueError, "paths"):
            validate_topology(peers, [], "router")
        validate_topology(peers, [Route("one", "u", "d1"), Route("two", "u", "d2")], "router")

    def test_one_path_gain_cannot_hide_required_regression(self):
        base = trial("base", {"a": 100, "b": 100})
        bad = trial("bbr", {"a": 150, "b": 80}, retrans=1)
        self.assertIs(choose([bad], base, {"a", "b"}, Policy()), base)

    def test_near_best_prefers_lower_normalized_retransmission(self):
        base = trial("base", {"a": 100, "b": 100}, retrans=100)
        fast = trial("fast", {"a": 125, "b": 125}, retrans=200)
        clean = trial("clean", {"a": 121, "b": 121}, retrans=10)
        self.assertEqual(choose([fast, clean], base, {"a", "b"}, Policy()).config.congestion, "clean")

    def test_missing_metric_and_no_reliable_gain_keep_baseline(self):
        base = trial("base", {"a": 100}, retrans=10)
        unknown = Trial(HostConfig("x", "fq", 1, 1), [Sample("a", "forward", 101, None, None)], "test")
        self.assertIs(choose([unknown], base, {"a"}, Policy()), base)

    def test_partial_failure_is_rejected(self):
        base = trial("base", {"a": 100, "b": 100})
        partial = trial("x", {"a": 130}, retrans=1)
        self.assertIs(choose([partial], base, {"a", "b"}, Policy()), base)


if __name__ == "__main__":
    unittest.main()
