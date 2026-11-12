import unittest,tempfile
from pathlib import Path
from unittest.mock import patch
import organize
class Tests(unittest.TestCase):
 def test_missing_metadata_fails_closed(self):
  with tempfile.TemporaryDirectory() as d,patch.object(organize,'BACKUP',Path(d)/'missing'):
   with self.assertRaises(RuntimeError):organize.torrent_paths(organize.DEFAULT)
 def test_unmanaged_root_fails(self):
  with self.assertRaises(ValueError):organize.torrent_paths(Path('/different'))
 def test_filename(self):self.assertEqual(organize.clean('Some [Video]'),'Some Video')
