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
    def test_edition_publish_freezes_counts_and_keeps_history_readonly(self):
        variant=self.db.create_variant(self.passage,self.w2,"春水东流，故人南去。","按语义补足",self.editor,0)
        self.db.add_note(variant,"补字参照纸背墨迹",self.editor)
        first=self.db.publish_edition(self.work,"评审会首轮交付",self.owner)
        self.assertEqual(1,first["version_no"])
        self.assertEqual(2,first["alignment_count"])
        self.assertEqual(1,first["variant_count"])
        self.assertEqual(1,first["note_count"])
        self.assertEqual(1,first["gap_count"])
        self.assertEqual({str(self.passage):1},first["revision_heads"])
        # 历史定本只读：定稿后再改草稿，旧定本内容保持不变
        self.db.update_variant(variant,"春水东流，[不可辨]人南去。","墨迹受损不再补写",self.editor,1)
        old=self.db.get_edition(first["id"],self.reviewer)
        self.assertTrue(old["readonly"])
        self.assertEqual("春水东流，故人南去。",old["collation"]["passages"][0]["variants"][0]["proposed_text"])
        listing=self.db.list_editions(self.work,self.reviewer)
        self.assertEqual(1,len(listing["editions"]))
        self.assertTrue(listing["has_draft_changes"])
        self.assertEqual(1,len(listing["draft_revisions"]))
        kinds=[(item["kind"],item.get("is_draft")) for item in listing["timeline"]]
        self.assertEqual([("revision",False),("edition",None),("revision",True)],kinds)
        # 基于更新后的修订发布 v2，异文数随草稿更新；缺口仍来自含[缺页]的对齐
        second=self.db.publish_edition(self.work,"评审会第二轮定稿",self.owner)
        self.assertEqual(2,second["version_no"])
        self.assertEqual(1,second["gap_count"])
        second_view=self.db.get_edition(second["id"],self.owner)
        self.assertEqual("春水东流，[不可辨]人南去。",second_view["collation"]["passages"][0]["variants"][0]["proposed_text"])
        # 时间线：v1冻结修订1 → v1 → v2冻结修订2 → v2，无遗留草稿
        tl=self.db.list_editions(self.work,self.owner)["timeline"]
        self.assertEqual([("revision",False),("edition",None),("revision",False),("edition",None)],
                         [(i["kind"],i.get("is_draft")) for i in tl])
        self.assertFalse(self.db.list_editions(self.work,self.owner)["has_draft_changes"])
        # 旧定本仍可回看，且仍是发布当时的内容
        old_again=self.db.get_edition(first["id"],self.owner)
        self.assertEqual(1,old_again["collation"]["gap_count"])
        self.assertEqual(2,self.db.list_editions(self.work,self.owner)["editions"][1]["version_no"])
    def test_edition_requires_owner_and_new_revisions(self):
        first=self.db.publish_edition(self.work,"首轮",self.owner)
        with self.assertRaisesRegex(DomainError,"新修订"):
            self.db.publish_edition(self.work,"没有新修订也发",self.owner)
        with self.assertRaisesRegex(DomainError,"负责人"):
            self.db.publish_edition(self.work,"编辑越权发布",self.editor)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.list_editions(self.work,self.outsider)
        with self.assertRaisesRegex(DomainError,"无权"):
            self.db.get_edition(first["id"],self.outsider)
        # 有了新修订后允许发布 v2
        vid=self.db.create_variant(self.passage,self.w2,"另一处补足","理由足够长",self.editor,0)
        self.assertIsInstance(vid,int)
        self.assertEqual(2,self.db.publish_edition(self.work,"二轮",self.owner)["version_no"])

if __name__=="__main__": unittest.main()
