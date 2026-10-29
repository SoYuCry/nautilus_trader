import unittest
from decimal import Decimal as D
from replay import measure
from sample_capacity import prune_snapshot,signal_at
from prepare import routine_temperature

class CapacityTests(unittest.TestCase):
    def test_unlimited_full_depth_with_fees(self):
        q,c,f,p=measure({9800:1000_000000,9900:2000_000000},.05,5)
        self.assertEqual(q,3000)
        self.assertAlmostEqual(c,2960)
        self.assertAlmostEqual(f,1.97)
        self.assertAlmostEqual(p,38.03)

    def test_pruning_deletes_only_contradictory_asks(self):
        original={9600:10_000000,9800:20_000000,9900:30_000000}
        pruned,n=prune_snapshot(original,D('.97'),D('.98'))
        self.assertEqual(pruned,{9800:20_000000,9900:30_000000})
        self.assertEqual(n,1)
        self.assertEqual(len(original),3)

    def test_missing_boundary_does_not_invent_quantity(self):
        pruned,n=prune_snapshot({9600:10_000000,9900:30_000000},D('.97'),D('.98'))
        self.assertEqual(pruned,{9900:30_000000})
        self.assertNotIn(9800,pruned)

    def test_invalid_bbo_is_not_accepted(self):
        for bid,ask in [(None,D('.99')),(D('.99'),D('.98')),(D('.99'),D('.99'))]:
            self.assertIsNone(prune_snapshot({9900:10_000000},bid,ask)[0])

    def test_source_availability_and_freshness(self):
        e={'start':0,'end':100,'weather_times':[20,50],'weather':[{'time':20,'maximum':25,'expires':40},{'time':50,'maximum':24,'expires':90}]}
        self.assertIsNone(signal_at(e,19));self.assertEqual(signal_at(e,20),25)
        self.assertIsNone(signal_at(e,40));self.assertEqual(signal_at(e,50),24)
        self.assertIsNone(signal_at(e,100))

    def test_price_edge_and_exchange_minimum(self):
        self.assertEqual(measure({10000:100_000000},0,5)[0],0)
        self.assertEqual(measure({9900:4_000000},0,5)[0],0)
        self.assertEqual(measure({9900:500_000000},0,5)[0],500)

    def test_routine_temperature_uses_precision_and_rejects_speci(self):
        self.assertEqual(routine_temperature('METAR KATL 231200Z 00000KT 10SM 18/12 RMK T01780122','KATL'),D('17.8'))
        self.assertIsNone(routine_temperature('SPECI KATL 231200Z 00000KT 10SM 18/12','KATL'))

if __name__=='__main__':unittest.main()
