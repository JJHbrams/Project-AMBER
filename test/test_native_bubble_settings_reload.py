import unittest
from copy import deepcopy
from unittest.mock import Mock, patch
from overlay.main import OverlayApp, _bubble_presentation_only_change


class NativeSettingsReloadTests(unittest.TestCase):
    def test_actual_reload_callback_preserves_session_on_font_change(self):
        old={'bubble':{'font_family':'Arial','font_size':12},'terminal':{'base_font_size':8}}
        new=deepcopy(old);new['bubble']['font_size']=18
        app=OverlayApp.__new__(OverlayApp)
        app._runtime_config_snapshot=old
        for name in ('character','chat','_bubble_session','_native_bubble_host','_bubble_manager','_bubble_input','_overlay_events','_initiative'):
            setattr(app,name,Mock())
        for name in ('_apply_tunnels','_set_provider_model','_publish_external_size','_terminate_managed_process'):
            setattr(app,name,Mock())
        app._is_dashboard_enabled=Mock(return_value=False)
        session=app._bubble_session
        with patch('overlay.main.load_cfg',return_value=new):
            app._reload_config()
        app.chat.kill.assert_not_called()
        session.stop.assert_not_called()
        self.assertIs(app._bubble_session,session)
        app._set_provider_model.assert_not_called()
        app._native_bubble_host.update_cfg.assert_called_once_with(new['bubble'],terminal_cfg=new['terminal'])

    def test_provider_change_is_not_visual_only(self):
        self.assertFalse(_bubble_presentation_only_change({'cli':{'provider':'codex'}},{'cli':{'provider':'claude-code'}}))

    def test_bundled_drag_refreshes_native_anchor(self):
        app=OverlayApp.__new__(OverlayApp);app._native_bubble_host=Mock();app._overlay_events=Mock()
        app._on_bundled_pointer_event('drag_move',{'x':100,'y':50})
        app._native_bubble_host.refresh_positions.assert_called_once()


if __name__=='__main__':unittest.main()
