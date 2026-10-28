"""Regression checks for the optional, public Chinook dataset mode."""

from pathlib import Path
import tempfile
import unittest

from auroradb.engine import Database
from auroradb.server import resource_directory


class ChinookDatasetTests(unittest.TestCase):
    def test_portable_script_loads_with_expected_counts(self):
        script = (resource_directory() / "examples" / "chinook.sql").read_text(encoding="utf-8-sig")
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "db")
            try:
                db.execute(script)
                counts = {}
                for table in (
                    "Album", "Artist", "Customer", "Employee", "Genre", "Invoice",
                    "InvoiceLine", "MediaType", "Playlist", "PlaylistTrack", "Track",
                ):
                    counts[table] = db.execute(f"SELECT COUNT(*) FROM {table}")[0]["rows"][0][0]
                self.assertEqual(counts, {
                    "Album": 347, "Artist": 275, "Customer": 59, "Employee": 8,
                    "Genre": 25, "Invoice": 412, "InvoiceLine": 2240,
                    "MediaType": 5, "Playlist": 18, "PlaylistTrack": 8715,
                    "Track": 3503,
                })
                result = db.execute(
                    "SELECT ar.Name AS artist, COUNT(*) AS tracks "
                    "FROM Artist ar JOIN Album al ON ar.ArtistId=al.ArtistId "
                    "JOIN Track t ON al.AlbumId=t.AlbumId "
                    "GROUP BY ar.Name ORDER BY tracks DESC, artist LIMIT 5;"
                )[0]
                self.assertEqual(result["row_count"], 5)
                self.assertEqual(result["rows"][0][0], "Iron Maiden")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
