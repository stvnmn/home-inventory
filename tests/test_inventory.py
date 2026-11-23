import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app import main


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db_path = main.DB_PATH
        main.DB_PATH = str(Path(self.temp.name) / "inventory.db")
        main.init_db()
        self.client = TestClient(main.app)
        response = self.client.post("/api/auth/signup", json={"username": "test", "password": "password"})
        self.assertEqual(response.status_code, 200)

    def tearDown(self):
        self.client.close()
        main.DB_PATH = self.old_db_path
        self.temp.cleanup()

    def test_edit_individual_expirations(self):
        product = self.client.post("/api/products", json={"name": "Milk", "category": "Fridge"}).json()["id"]
        response = self.client.post("/api/stock", json={
            "product_id": product, "quantity": 2, "expires_on": "2030-01-01",
        })
        self.assertEqual(response.status_code, 200)
        units = self.client.get(f"/api/products/{product}/units").json()
        first, second = units[0]["id"], units[1]["id"]
        with closing(main.db()) as c:
            c.execute("INSERT INTO notified VALUES (?,?)", (f"expiry:{first}", "2029-12-01"))
            c.commit()

        self.assertEqual(self.client.patch(f"/api/units/{first}", json={"expires_on": "2030-02-01"}).status_code, 200)
        units = self.client.get(f"/api/products/{product}/units").json()
        self.assertEqual([u["expires_on"] for u in units], ["2030-02-01", "2030-01-01"])
        self.assertEqual(self.client.get("/api/products").json()[0]["next_expiry"], "2030-01-01")
        with closing(main.db()) as c:
            self.assertIsNone(c.execute("SELECT 1 FROM notified WHERE key=?", (f"expiry:{first}",)).fetchone())
            c.execute("INSERT INTO notified VALUES (?,?)", (f"expiry:{first}", "2029-12-01"))
            c.commit()
        self.client.patch(f"/api/units/{first}", json={"expires_on": "2030-02-01"})
        with closing(main.db()) as c:
            self.assertIsNotNone(c.execute("SELECT 1 FROM notified WHERE key=?", (f"expiry:{first}",)).fetchone())

        self.assertEqual(self.client.patch(f"/api/units/{second}", json={"expires_on": None}).status_code, 200)
        self.assertEqual(self.client.get("/api/products").json()[0]["next_expiry"], "2030-02-01")
        self.assertEqual(self.client.patch(f"/api/units/{second}", json={"expires_on": "invalid"}).status_code, 422)
        self.assertEqual(self.client.patch(f"/api/units/{second}", json={}).status_code, 422)
        self.assertEqual(self.client.get("/api/products/9999/units").status_code, 404)
        self.client.post("/api/finish", json={"product_id": product})
        self.assertEqual(self.client.patch(f"/api/units/{first}", json={"expires_on": None}).status_code, 404)
        self.assertEqual(len(self.client.get(f"/api/products/{product}/units").json()), 1)

    def test_usage_and_restock_threshold(self):
        product = self.client.post("/api/products", json={"name": "Soap"}).json()["id"]
        with closing(main.db()) as c:
            c.executemany(
                "INSERT INTO units (product_id, added_on, opened_on, finished_on, discarded) VALUES (?,?,?,?,?)",
                [
                    (product, "2025-01-01", "2025-01-01", "2025-01-11", 0),
                    (product, "2025-01-01", None, "2025-01-31", 0),
                    (product, "2025-01-01", None, "2025-02-01", 1),
                    (product, "2025-02-01", "2025-02-01", None, 0),
                    (product, "2025-02-01", None, None, 0),
                ],
            )
            c.commit()
            before = main.product_stats(c, product, date(2025, 2, 16), 14)
            on = main.product_stats(c, product, date(2025, 2, 17), 14)
            self.assertEqual(before["avg_days_per_unit"], 15)
            self.assertEqual(before["units_finished"], 2)
            self.assertEqual(before["predicted_runout"], "2025-03-03")
            self.assertEqual(before["restock_on"], "2025-02-17")
            self.assertFalse(before["needs_restock"])
            self.assertTrue(on["needs_restock"])
            c.execute("UPDATE units SET finished_on='2025-02-18' WHERE finished_on IS NULL")
            self.assertTrue(main.product_stats(c, product, date(2025, 2, 18), 14)["needs_restock"])


if __name__ == "__main__":
    unittest.main()
