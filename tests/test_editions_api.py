import json, os, tempfile, threading, unittest
from http.client import HTTPConnection
from pathlib import Path
from http.server import ThreadingHTTPServer


def setUpModule():
    fd, EditionApiTest.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    os.environ["COLLATION_DB"] = EditionApiTest.path
    import app as app_module
    EditionApiTest.app = app_module
    EditionApiTest.server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
    EditionApiTest.port = EditionApiTest.server.server_address[1]
    EditionApiTest.thread = threading.Thread(target=EditionApiTest.server.serve_forever, daemon=True)
    EditionApiTest.thread.start()


def tearDownModule():
    EditionApiTest.server.shutdown(); EditionApiTest.server.server_close()
    try: os.unlink(EditionApiTest.path)
    except OSError: pass


class EditionApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = cls.app.Handler.db
        cls.owner = cls.db.add_user("接口负责人", "owner")
        cls.editor = cls.db.add_user("接口编辑", "editor")
        cls.work = cls.db.create_work("接口残卷", "定本接口测试", cls.owner)
        cls.w = cls.db.add_witness(cls.work, "甲", "version")
        cls.p = cls.db.add_passage(cls.work, "第一节", "原文一句。", cls.owner)
        cls.db.align_passage(cls.p, cls.w, "原文[缺页]", 1, cls.owner)
        cls.v = cls.db.create_variant(cls.p, cls.w, "原文一句。", "补足缺页内容", cls.owner, 0)

    def req(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, payload, {"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse(); data = json.loads(resp.read().decode()); conn.close()
        return resp.status, data

    def test_publish_list_get_and_page(self):
        status, data = self.req("POST", f"/api/works/{self.work}/editions",
                                {"note": "首轮交付", "user_id": self.owner})
        self.assertEqual(201, status); self.assertTrue(data["ok"])
        edition = data["edition"]
        self.assertEqual(1, edition["version_no"])
        self.assertEqual(1, edition["gap_count"])
        self.assertEqual(1, edition["variant_count"])

        status, listing = self.req("GET", f"/api/works/{self.work}/editions?user_id={self.owner}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(listing["editions"]))
        self.assertTrue(listing["editions"][0]["readonly"])
        kinds = [item["kind"] for item in listing["timeline"]]
        self.assertIn("edition", kinds)

        status, view = self.req("GET", f"/api/editions/{edition['id']}?user_id={self.owner}")
        self.assertEqual(200, status); self.assertTrue(view["readonly"])
        self.assertEqual("首轮交付", view["note"])
        self.assertEqual(1, view["collation"]["gap_count"])

        # 无新修订拒绝重复发布
        status, dup = self.req("POST", f"/api/works/{self.work}/editions",
                               {"note": "重复", "user_id": self.owner})
        self.assertEqual(400, status); self.assertIn("新修订", dup["error"])

        # 页面已接通定本区块
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/"); resp = conn.getresponse()
        html = resp.read().decode(); conn.close()
        self.assertEqual(200, resp.status)
        self.assertIn("定本发布", html)
        self.assertIn("/api/works/", html)


if __name__ == "__main__":
    unittest.main()
