"""The pinned external renderer version must have exactly one source.

It used to be written as a literal in four places across the Inno Setup script
and configure.ps1, so it went stale one release after it was written — the same
way test_runtime_contract.py had pinned "1.5.6". These tests count that cause
rather than the symptom: they fail if a literal version reappears, or if the
single source stops being wired through to the installer.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer"
PIN = INSTALLER / "external-overlay.pin"
ISS = INSTALLER / "engram-overlay.iss"
BUILD = INSTALLER / "build-installer.ps1"
CONFIGURE = INSTALLER / "configure.ps1"
BUILD_COMPONENTS = INSTALLER / "build-components.ps1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


class ExternalOverlayPinTests(unittest.TestCase):
    def test_pin_file_holds_a_single_well_formed_version(self):
        self.assertTrue(PIN.is_file(), "installer/external-overlay.pin is missing")
        value = read(PIN).strip()
        self.assertRegex(value, r"^\d+\.\d+\.\d+(\.\d+)?$")
        self.assertNotIn("\n", value, "the pin holds one version and nothing else")

    def test_no_installer_file_hardcodes_an_external_overlay_version(self):
        # A bare version literal next to "릴리스"/"release" wording is what rotted.
        literal = re.compile(r"v\d+\.\d+\.\d+\.\d+")
        for path in (ISS, CONFIGURE, BUILD):
            for number, line in enumerate(read(path).splitlines(), start=1):
                if "ExternalOverlay" not in line and "external-overlay" not in line:
                    continue
                found = literal.search(line)
                self.assertIsNone(
                    found,
                    f"{path.name}:{number} hardcodes {found.group(0) if found else ''} — "
                    "read installer/external-overlay.pin instead",
                )

    def test_iss_declares_the_define_with_a_visible_fallback(self):
        source = read(ISS)
        self.assertIn("#ifndef ExternalOverlayVersion", source)
        # "unpinned" is deliberately not a version: an unwired build must look
        # wrong on screen rather than claim a plausible one.
        self.assertIn('#define ExternalOverlayVersion "unpinned"', source)
        self.assertIn("{#ExternalOverlayVersion}", source)

    def test_build_script_passes_the_pin_to_iscc(self):
        source = read(BUILD)
        self.assertIn("external-overlay.pin", source)
        self.assertIn("/DExternalOverlayVersion=", source)

    def test_pin_is_shipped_so_configure_can_read_it_after_install(self):
        # configure.ps1 runs from {app}\installer at install time; the pin has to
        # be there too or every installed copy reports "unpinned".
        self.assertIn('Source: "external-overlay.pin"; DestDir: "{app}' + chr(92) + 'installer"', read(ISS))
        # configure.ps1 delegates the read to external-overlay.ps1, which is the
        # only place that knows the file's name and format.
        configure = read(CONFIGURE)
        self.assertIn("external-overlay.ps1", configure)
        self.assertIn("Get-PinnedExternalOverlayVersion", configure)
        self.assertIn("external-overlay.pin", read(ROOT / "installer" / "external-overlay.ps1"))

    def test_configure_reports_the_pin_it_read(self):
        source = read(CONFIGURE)
        self.assertIn("$PinnedExternalOverlayVersion", source)
        self.assertIn("v$PinnedExternalOverlayVersion", source)



class WizardWiringTests(unittest.TestCase):
    """The wizard must ask a question the installing user can answer.

    It used to ask for a distribution shape ("Preset provider / Renderer SDK")
    with two options permanently disabled. See the plan §2.
    """

    def test_page_asks_for_a_character_not_a_distribution_shape(self):
        source = read(ISS)
        self.assertIn('[Components]', source)
        self.assertIn('현재 캐릭터를 변경하지 않음', source)
        self.assertIn('external-components\\tree.iss', source)
        for gone in ("Preset provider", "Renderer SDK", "현재 릴리스에서 사용 불가"):
            self.assertNotIn(gone, source, f"{gone!r} was the unanswerable question")

    def test_sdk_is_a_checkbox_not_a_radio_option(self):
        # Choosing a character and preparing a dev environment are not mutually
        # exclusive; a radio group forced them to be.
        source = read(ISS)
        self.assertIn('Name: "sdk";', source)
        self.assertIn("WizardIsComponentSelected('sdk')", source)
        self.assertNotIn('Flags: exclusive', source)

    def test_selection_and_sdk_flag_both_reach_configure(self):
        source = read(ISS)
        self.assertIn("-ExternalOverlayMode ", source)
        self.assertIn("-ExternalOverlaySdk ", source)
        self.assertIn("$ExternalOverlaySdk", read(CONFIGURE))

    def test_wizard_does_not_predict_the_install_outcome(self):
        # configure.ps1 decides (Python eligibility, existing ownership), so the
        # wizard must not assert a result it cannot know.
        source = read(ISS)
        self.assertNotIn("설치되지 않았습니다", source)
        self.assertNotIn("MsgBox('선택한 외부 오버레이", source)

    def test_user_owned_runtime_defaults_to_native_without_claiming_migration(self):
        source = read(ISS)
        marker = "ExistingExternalComponent('ownership=user-owned')"
        default = "WizardSelectComponents('native')"
        self.assertIn(marker, source)
        self.assertIn(default, source)
        self.assertLess(source.index(marker), source.index(default))
        self.assertIn("CurPageID = wpSelectComponents", source)
        self.assertIn("PreserveUserOwnedExternalDefault and WizardSilent", source)
        self.assertLess(source.index("procedure CurPageChanged"), source.index("function PrepareToInstall"))
        generator = read(BUILD_COMPONENTS)
        guard = "if PreserveUserOwnedExternalDefault then Exit"
        self.assertIn(guard, generator)

    def test_configure_reports_every_outcome_to_the_user(self):
        source = read(CONFIGURE)
        self.assertIn('Initialize-EngramExternalRuntime -Mode $ExternalOverlayMode', source)
        helper = read(CONFIGURE.parent / 'joint-startup.ps1')
        for outcome in ('External renderer installed:', 'Reusing validated external runtime;',
                        'Requested external runtime could not be installed:'):
            self.assertIn(outcome, helper)
        self.assertIn("throw ('Requested external runtime", helper,
                      'explicit external failure must not be presented as a successful fallback')

    def test_build_requires_verified_selective_bundle_not_empty_placeholder(self):
        # Selected components are a required setup result; an empty placeholder
        # cannot satisfy the new actual-payload installation contract.
        source = read(BUILD)
        self.assertIn("Write-EngramComponentIncludes", source)
        self.assertIn('external-components\\files.iss', read(ISS))
        self.assertNotIn('Source: "external-overlay.whl"', read(ISS))
        self.assertIn("external-overlay.ps1", read(ISS))

if __name__ == "__main__":
    unittest.main()
