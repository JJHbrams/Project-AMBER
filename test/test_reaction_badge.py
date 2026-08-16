import unittest

from PIL import Image

from overlay.reaction_badge import (
    CELL_HEIGHT,
    CELL_WIDTH,
    SHEET_COLUMNS,
    SHEET_ROWS,
    classify_reaction,
    crop_sprite,
    key_chroma,
    public_event,
    is_engram_character,
    is_reaction_pack_applicable,
    reaction_character_identity,
    validate_sheet,
)


def _sheet():
    image = Image.new("RGBA", (CELL_WIDTH * SHEET_COLUMNS, CELL_HEIGHT * SHEET_ROWS), "#00ff00")
    image.putpixel((CELL_WIDTH + 7, 9), (1, 2, 3, 255))
    return image


class ReactionBadgeTests(unittest.TestCase):
    def test_engram_path_name_normalizes_to_stem(self):
        self.assertTrue(is_engram_character("C:/work/resource/character/engram.png"))
        self.assertTrue(is_engram_character("resource/character/engram.png"))
        self.assertFalse(is_engram_character("resource/character/other.png"))

    def test_reaction_pack_identity_uses_set_when_name_empty(self):
        self.assertEqual(reaction_character_identity({"name": "", "set": "engram"}), "engram")
        self.assertTrue(is_reaction_pack_applicable(reaction_character_identity({"set": "engram"}), "engram", False))
        self.assertFalse(is_reaction_pack_applicable("C:/resource/custom.png", "engram", False))
        self.assertTrue(is_reaction_pack_applicable("C:/resource/custom.png", "engram", True))

    def test_classifier_maps_public_events(self):
        cases = (
            ({"kind": "thought", "text": "deep reasoning"}, 20),
            ({"kind": "tool_use", "tool_name": "Read"}, 3),
            ({"kind": "tool_result", "tool_output": "ok"}, 2),
            ({"kind": "error", "text": "permission denied"}, 14),
            ({"kind": "result", "is_error": True}, 17),
            ({"kind": "thought", "text": "재시도 대기"}, 12),
        )
        for event, index in cases:
            with self.subTest(event=event):
                self.assertEqual(classify_reaction(event).index, index)

    def test_classifier_ignores_speech_and_private_fields(self):
        event = {"kind": "speech", "text": "deep", "thinking": "private chain of thought"}
        self.assertEqual(public_event(event), {"kind": "speech", "text": "deep"})
        self.assertIsNone(classify_reaction(event))

    def test_crop_coordinates_and_chroma_alpha(self):
        sprite = crop_sprite(_sheet(), 1)
        self.assertEqual(sprite.size, (CELL_WIDTH, CELL_HEIGHT))
        self.assertEqual(sprite.getpixel((7, 9)), (1, 2, 3, 255))
        keyed = key_chroma(sprite)
        self.assertEqual(keyed.getpixel((0, 0))[3], 0)
        self.assertEqual(keyed.getpixel((7, 9)), (1, 2, 3, 255))

    def test_invalid_sheet_is_rejected(self):
        invalid = Image.new("RGBA", (10, 10))
        self.assertFalse(validate_sheet(invalid))
        with self.assertRaises(ValueError):
            crop_sprite(invalid, 0)


if __name__ == "__main__":
    unittest.main()
