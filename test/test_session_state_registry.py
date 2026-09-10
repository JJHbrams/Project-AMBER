import unittest

from overlay.session_registry import SessionStateRegistry


class SessionStateRegistryTests(unittest.TestCase):
    def test_explicit_local_label_validates_without_state_or_presence_mutation(self):
        row=self.registry.upsert({'provider':'mcp','session_id':'a','state':'needs_input'})
        self.now+=1
        self.assertTrue(self.registry.set_label(row['key'],'직접 정한 이름'))
        changed=self.registry.snapshot()[0]
        self.assertEqual({k:v for k,v in changed.items() if k!='label'},
                         {k:v for k,v in row.items() if k!='label'})
        for invalid in ('C:/private','x'*129,{'raw':'content'}):
            self.assertFalse(self.registry.set_label(row['key'],invalid))
        self.registry.presence({'provider':'mcp','session_id':'a'})
        self.assertEqual(self.registry.snapshot()[0]['label'],'직접 정한 이름')
        self.now+=3601
        self.assertFalse(self.registry.set_label(row['key'],'stale'))
        self.assertEqual(self.registry.snapshot(),[])

    def test_user_title_wins_over_producer_refresh_and_blank_restores_latest(self):
        row = self.registry.upsert({'provider':'mcp','session_id':'a','state':'working',
                                    'label':'Producer first'})
        self.assertTrue(self.registry.set_label(row['key'], '사용자 제목'))
        self.now += 1
        refreshed = self.registry.upsert({'provider':'mcp','session_id':'a','state':'ready',
                                          'label':'Producer refreshed'})
        self.assertEqual(refreshed['label'], '사용자 제목')
        self.assertTrue(self.registry.set_producer_label('mcp', 'a', 'Agent task summary'))
        self.assertEqual(self.registry.snapshot()[0]['label'], '사용자 제목')
        self.assertTrue(self.registry.set_label(row['key'], None))
        self.assertEqual(self.registry.snapshot()[0]['label'], 'Agent task summary')
        self.registry.remove('mcp', 'a')
        self.registry.upsert({'provider':'mcp','session_id':'a','state':'unknown',
                              'label':'Fresh session title'})
        self.assertEqual(self.registry.snapshot()[0]['label'], 'Fresh session title')

    def test_alias_and_prune_clear_title_metadata(self):
        self.registry.claim({'provider':'claude','session_id':'owner','state':'working'})
        self.registry.upsert({'provider':'claude','session_id':'alias','state':'unknown',
                              'label':'Alias title'})
        self.assertTrue(self.registry.set_label('claude:alias', '사용자 별칭'))
        self.registry.bind_alias('claude', 'alias', 'owner')
        self.assertTrue(self.registry.set_producer_label('claude', 'owner', 'Owner generated title'))
        self.assertEqual(self.registry.snapshot()[0]['label'], 'Owner generated title')
        self.now += 3601
        self.assertEqual(self.registry.snapshot(), [])

    def setUp(self):
        self.now = 10.0
        self.registry = SessionStateRegistry(clock=lambda: self.now)

    def test_identity_clock_and_heartbeat_semantics(self):
        first = self.registry.upsert({"provider": "claude", "session_id": "same", "state": "working"})
        self.now = 12.0
        heartbeat = self.registry.upsert({"provider": "claude", "session_id": "same", "state": "working"})
        self.assertEqual((first["first_seen"], first["state_since"]), (10.0, 10.0))
        self.assertEqual((heartbeat["first_seen"], heartbeat["state_since"], heartbeat["last_seen"]), (10.0, 10.0, 12.0))
        self.now = 14.0
        changed = self.registry.upsert({"provider": "claude", "session_id": "same", "state": "ready"})
        self.assertEqual((changed["first_seen"], changed["state_since"], changed["last_seen"]), (10.0, 14.0, 14.0))

    def test_provider_namespace_stable_order_and_idle_reentry(self):
        self.registry.upsert({"provider": "claude", "session_id": "same", "state": "working"})
        self.now = 11.0
        self.registry.upsert({"provider": "codex", "session_id": "same", "state": "ready"})
        self.now = 12.0
        self.registry.upsert({"provider": "claude", "session_id": "same", "state": "blocked"})
        self.assertEqual([item["key"] for item in self.registry.snapshot()], ["claude:same", "codex:same"])
        self.now = 3612.0
        self.assertEqual(self.registry.snapshot(), [])
        reentered = self.registry.upsert({"provider": "claude", "session_id": "same", "state": "working"})
        self.assertEqual(reentered["first_seen"], 3612.0)

    def test_reverse_heartbeats_do_not_reorder_rows(self):
        self.registry.upsert({"provider": "claude", "session_id": "first", "state": "working"})
        self.now = 11.0
        self.registry.upsert({"provider": "claude", "session_id": "second", "state": "ready"})
        self.now = 12.0
        self.registry.upsert({"provider": "claude", "session_id": "second", "state": "ready"})
        self.now = 13.0
        self.registry.upsert({"provider": "claude", "session_id": "first", "state": "working"})
        self.assertEqual([item["session_id"] for item in self.registry.snapshot()], ["first", "second"])

    def test_state_age_does_not_expire_and_idle_boundary_is_exact(self):
        self.registry.upsert({"provider": "claude", "session_id": "long", "state": "working"})
        self.now = 500.0
        self.registry.upsert({"provider": "claude", "session_id": "long", "state": "working"})
        self.now = 1000.0
        self.registry.upsert({"provider": "claude", "session_id": "long", "state": "working"})
        self.now = 1201.0
        self.registry.upsert({"provider": "claude", "session_id": "long", "state": "working"})
        self.assertEqual(self.registry.snapshot()[0]["state_since"], 10.0)
        self.now = 4800.999
        self.assertEqual(len(self.registry.snapshot()), 1)
        self.now = 4801.0
        self.assertEqual(self.registry.snapshot(), [])
        reentry = self.registry.upsert({"provider": "claude", "session_id": "long", "state": "ready"})
        self.assertEqual(reentry["first_seen"], 4801.0)


if __name__ == "__main__":
    unittest.main()
