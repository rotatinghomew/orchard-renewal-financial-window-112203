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

    def test_reject_wrong_container(self):
        with TemporaryDirectory() as folder:
            packet = Path(folder) / "unexpected.json"
            packet.write_text(json.dumps({"invalid": 3}), encoding="utf-8")
            with self.assertRaises(ValueError):
                read_packet(packet)
