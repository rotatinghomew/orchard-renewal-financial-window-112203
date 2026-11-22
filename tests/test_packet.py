"""核对演练资料的公开输入约定。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from inspect_packet import read_packet


class PacketTest(unittest.TestCase):
    def test_related_records(self):
        doc = read_packet(Path("资料/演练.json"))
        ids={(row['orchard'],row['year']) for row in doc['history']}
        self.assertTrue(len(ids)==len(doc['history']))
        self.assertTrue({(row['orchard'],row['year']) for row in doc['amendments']} <= ids)
        self.assertTrue({row['block'] for row in doc['commitments']} <= {row['block'] for row in doc['parcels']})
        self.assertTrue({row['lot'] for row in doc['commitments']} <= {row['lot'] for row in doc['stock']})
        for scenario in {row['id'] for row in doc['scenarios']}:
            self.assertTrue(len({(row['year'],row['region']) for row in doc['scenarios'] if row['id']==scenario}) == 14)
        self.assertTrue({row['house'] for row in doc['support']}=={row['house'] for row in doc['parcels']})

    def test_complete_history_covers_the_renewal_horizon(self):
        packet = read_packet(Path("资料/演练.json"))
        start, end = packet['pool']['horizon']
        support = {}
        for row in packet['history']:
            if row['final']:
                key = row['region'], row['root'], row['scion'], row['year'] - row['planted']
                support.setdefault(key, set()).add(row['orchard'])
        for root in ('R1', 'R2'):
            self.assertTrue(all(len(support['east', root, 'S1', age]) >= packet['pool']['min_analogs']
                                for age in range(end - start + 1)))
        quotes = [row for row in packet['commitments'] if row['kind'] == 'quote']
        stock = {row['lot']: row['count'] for row in packet['stock']}
        self.assertTrue(any(sum(row['count'] for row in quotes if row['lot'] == lot) > count
                            for lot, count in stock.items()))

    def test_reject_wrong_container(self):
        with TemporaryDirectory() as folder:
            packet = Path(folder) / "unexpected.json"
            packet.write_text(json.dumps({"invalid": 3}), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_packet(packet)
