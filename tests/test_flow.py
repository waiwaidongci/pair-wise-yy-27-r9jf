import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError

class CollationFlowTest(unittest.TestCase):
    def setUp(self):
        fd,self.path=tempfile.mkstemp(suffix=".db"); os.close(fd); self.db=CollationDB(self.path)
        self.owner=self.db.add_user("负责人","owner"); self.editor=self.db.add_user("编辑","editor"); self.reviewer=self.db.add_user("审阅","reviewer"); self.outsider=self.db.add_user("外部","reviewer")
        self.work=self.db.create_work("残卷","异文比较",self.owner)
        self.w1=self.db.add_witness(self.work,"甲本","version"); self.w2=self.db.add_witness(self.work,"乙本","fragment","馆藏残片","中段缺页")
        self.db.grant_witness_editor(self.w2,self.editor,self.owner); self.db.grant_work_access(self.work,self.reviewer,"view",self.owner)
        self.passage=self.db.add_passage(self.work,"第一节","春水东流，故人南去。",self.owner)
        self.db.align_passage(self.passage,self.w1,"春水东流，故人南去。",1,self.owner)
        self.db.align_passage(self.passage,self.w2,"春水东流，[缺页]",2,self.editor)
    def tearDown(self): self.db.close(); os.unlink(self.path)
    def test_multilayer_revision_snapshot_export_and_lock(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        rev=self.db.update_variant(variant,"春水东流，[不可辨]人南去。","墨迹受损，不再直接补写",self.editor,1)
        self.assertEqual(2,rev)
        snap=self.db.get_snapshot(self.passage,2,self.owner)
        self.assertEqual(2,snap["layer"])
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,exported["gap_count"])
        self.assertTrue(exported["passages"][0]["variants"][0]["notes"] == [])
        self.db.lock_passage(self.passage,self.owner,"定稿")
        with self.assertRaisesRegex(DomainError,"锁定"):
            self.db.update_variant(variant,"另一文本","无意义修改",self.editor,2)
    def test_optimistic_lock_permission_and_mark_validation(self):
        first=self.db.create_variant(self.passage,self.w2,"补足一","理由一",self.editor,0)
        with self.assertRaisesRegex(DomainError,"版本冲突"):
            self.db.create_variant(self.passage,self.w2,"补足二","理由二",self.editor,0)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.create_variant(self.passage,self.w2,"补足三","理由三",self.reviewer,1)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.export_collation(self.work,self.outsider)
        with self.assertRaisesRegex(DomainError,"括号"):
            self.db.align_passage(self.passage,self.w1,"文本[未闭合",9,self.owner)

    def test_final_edition_publish_immutability_and_draft_timeline(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        self.db.add_note(variant,"补字仍需核胶片。",self.editor)
        first=self.db.publish_final_edition(self.work,"1","评审会首版定稿",self.owner)
        self.assertEqual(1,first["version_no"])
        self.assertTrue(first["read_only"])
        self.assertEqual(2,first["alignment_count"])
        self.assertEqual(1,first["variant_count"])
        self.assertEqual(1,first["note_count"])
        self.assertEqual(1,first["gap_count"])
        self.assertEqual(1,first["content"]["passages"][0]["revision"])
        # 无新修订不能重复发布，避免旧内容冒充新定本
        with self.assertRaisesRegex(DomainError,"没有新的段落修订"):
            self.db.publish_final_edition(self.work,"2","重复发布",self.owner)
        # 编辑无权发布定本
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.publish_final_edition(self.work,"","编辑抢发",self.editor)
        # 版本号必须顺延
        with self.assertRaisesRegex(DomainError,"顺延"):
            self.db.publish_final_edition(self.work,"5","跳号",self.owner)
        # 继续修改形成新草稿后，基于更新的修订发布第二版
        self.db.update_variant(variant,"春水东流，[不可辨]人南去。","墨迹受损，不直接补写",self.editor,1)
        second=self.db.publish_final_edition(self.work,"","第二版自动顺延",self.owner)
        self.assertEqual(2,second["version_no"])
        self.assertEqual(2,second["content"]["passages"][0]["variants"][0]["layer"])
        # 历史定本只读回看，内容停留在发布当时
        listing=self.db.list_final_editions(self.work,self.reviewer)
        self.assertEqual([1,2],[e["version_no"] for e in listing["editions"]])
        self.assertTrue(all(e["read_only"] for e in listing["editions"]))
        self.assertEqual(2,listing["editions"][-1]["passages"][0]["revision"])
        frozen=self.db.get_final_edition(self.work,1,self.reviewer)
        self.assertEqual("春水东流，故人南去。",frozen["content"]["passages"][0]["variants"][0]["proposed_text"])
        # 导出（当前草稿）标注最近定本及定稿后的草稿修订先后
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(2,exported["latest_final_version"])
        self.assertTrue(exported["is_draft"])
        p=exported["passages"][0]
        self.assertEqual(2,p["revision_at_final"])
        self.assertEqual(0,p["draft_revisions_since_final"])
        self.db.update_variant(variant,"春水东流，又一稿。","评审会后再议",self.editor,2)
        exported=self.db.export_collation(self.work,self.reviewer)
        self.assertEqual(1,exported["passages"][0]["draft_revisions_since_final"])
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.get_final_edition(self.work,1,self.outsider)
        with self.assertRaisesRegex(DomainError,"定本不存在"):
            self.db.get_final_edition(self.work,9,self.owner)

    def test_edition_publish_detects_alignment_change(self):
        self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        self.db.publish_final_edition(self.work,"1","首版",self.owner)
        with self.assertRaisesRegex(DomainError,"没有新的段落修订"):
            self.db.publish_final_edition(self.work,"2","未改",self.owner)
        # 首版后新增一个版本对齐，也属于必须并入新定本的更新
        w3=self.db.add_witness(self.work,"丙本","transcription","新得抄本")
        self.db.align_passage(self.passage,w3,"春水东流，[残损]人南去。",3,self.owner)
        second=self.db.publish_final_edition(self.work,"2","补入丙本",self.owner)
        self.assertEqual(3,second["alignment_count"])
        self.assertEqual(2,second["gap_count"])

if __name__=="__main__": unittest.main()
