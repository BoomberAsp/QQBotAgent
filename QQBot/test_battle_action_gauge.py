from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from lib.gamedata_source import GameData
from tools import ag_skill_index, battle_parser
from tools.ag_trigger_engine import build_hypothesis


class BattleActionGaugeTest(TestCase):
    def test_lucia_extra_turn_triggers_one_neko_pull(self):
        lucia = ag_skill_index._parse_skill({
            "name": "强行复写",
            "type": "Active",
            "cd": "4",
            "des": "攻击敌人并附加沉睡。攻击后，生成额外回合。",
            "des2": "攻击敌人并附加沉睡。",
        }, "归还者璐茜娅", 1)
        neko = ag_skill_index._parse_skill({
            "name": "疯猫的混沌",
            "type": "Passive",
            "cd": "2",
            "des": "当敌人生成额外回合时，解除自身所有负面效果，随后自身行动值提升[25–40%]。",
        }, "猫祭", 1)
        base_pull = {
            "direction": "pull", "target": "self", "trigger": "conditional",
            "magnitude": {"kind": "fixed", "value": 25.0, "raw": "25%"},
        }
        with patch.object(ag_skill_index, "_gamedata_ag_effects", return_value=[base_pull]):
            ag_skill_index._merge_gamedata_effects([neko], "猫祭")
        self.assertFalse(neko.generates_extra_turn)

        team = [{"name": "归还者璐茜娅", "side": "enemy"}, {"name": "猫祭", "side": "ally"}]
        index = {"归还者璐茜娅": {"skills": [lucia]}, "猫祭": {"skills": [neko]}}
        observed = {
            "fresh": {("归还者璐茜娅", "enemy"): [1], ("猫祭", "ally"): [1]},
            "seen": {("归还者璐茜娅", "enemy"): True, ("猫祭", "ally"): True},
        }
        for cooldowns in (None, observed):
            with self.subTest(observed=bool(cooldowns)):
                result = build_hypothesis(team, "归还者璐茜娅", index, 1, cooldowns)
                self.assertTrue(result["first_skill_can_grant_extra_turn"])
                self.assertEqual(len(result["chain"]), 1)
                pull = result["chain"][0]
                self.assertEqual(pull["trigger"], "on_enemy_extra_turn")
                self.assertEqual(pull["magnitude"], "25%~40%")
                self.assertEqual(bool(pull.get("confirmed")), bool(cooldowns))

    def test_soul_burn_does_not_imply_an_extra_turn(self):
        skill = ag_skill_index._parse_skill({
            "des": "攻击敌人。",
            "burst": "施展后产生追加回合。",
        }, "米德", 2)
        self.assertFalse(skill.generates_extra_turn)

    def test_distinct_effects_with_same_base_value_are_preserved(self):
        skill = ag_skill_index.Skill(slot=1, ag_effects=[{
            "direction": "pull", "target": "self", "trigger": "on_enemy_extra_turn",
            "magnitude": {"kind": "range", "min": 25.0, "max": 40.0},
        }])
        base_pull = {
            "direction": "pull", "target": "self", "trigger": "conditional",
            "magnitude": {"kind": "fixed", "value": 25.0, "raw": "25%"},
        }
        distinct_pull = {**base_pull, "target": "all"}
        with patch.object(ag_skill_index, "_gamedata_ag_effects",
                          return_value=[base_pull, distinct_pull]):
            ag_skill_index._merge_gamedata_effects([skill], "示例角色")
        self.assertEqual([effect["target"] for effect in skill.ag_effects], ["self", "all"])

    def test_initial_zero_cannot_identify_first_actor(self):
        rows = [
            {"name": "米德", "side": "ally", "action_value": 0, "acting": False},
            {"name": "归还者璐茜娅", "side": "enemy", "action_value": 3, "acting": False},
        ]
        with Image.new("RGB", (1, 1)) as image:
            characters = battle_parser._post_process(rows, image)
        self.assertFalse(any(row["acting"] for row in characters))
        self.assertEqual(
            battle_parser._find_first_actor([{"phase": "pre", "characters": characters}]),
            (None, None),
        )

    def test_post_run_preserves_overflow_and_uses_unique_acting_row(self):
        rows = [
            {"name": "归还者璐茜娅", "side": "enemy", "action_value": 0, "acting": False},
            {"name": "米德", "side": "ally", "action_value": 87, "acting": False},
            {"name": "猫祭", "side": "ally", "action_value": 111, "acting": False},
        ]
        with Image.new("RGB", (1, 1)) as image:
            characters = battle_parser._post_process(rows, image)
        post = {"phase": "post", "characters": characters}
        self.assertEqual(battle_parser._find_first_actor([post]), ("归还者璐茜娅", "enemy"))
        self.assertEqual(next(row["action_value"] for row in characters if row["name"] == "猫祭"), 111)
        next(row for row in characters if row["name"] == "米德")["acting"] = True
        self.assertEqual(battle_parser._find_first_actor([post]), (None, None))


class PreRunValidityTest(TestCase):
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.source = GameData(directory)
        self.source.roles = {"H183": {}, "H165": {}}
        self.source.status = {
            "Stealth": {"has_row_icon": True},
            "Knowledge": {"has_row_icon": True},
        }
        self.enterContext(patch.object(battle_parser, "_gamedata", return_value=self.source))
        self.enterContext(patch.object(battle_parser, "_rule1_mode", return_value="on"))
        self.enterContext(patch.object(battle_parser, "_log_rule1_shadow"))

    def validate(self, role, detected=(), source="gamedata"):
        return battle_parser._validate_pre_screenshot({
            "characters": [{"name": role, "action_value": 1}],
            "row_buffs": [dict.fromkeys(detected, 1)],
            "buff_source": source,
        }, None)

    def test_mead_absence_is_valid_even_in_strict_mode(self):
        self.source.role_grants["H183"] = {
            "pre_grants": ["Stealth"],
            "grants": [{
                "id": "Stealth", "entry": True, "phase": "pre",
                "advisory": False, "rate": 0.7,
            }],
        }
        self.assertEqual(self.validate("H183"), (True, []))

    def test_guaranteed_start_status_still_checked_in_strict_mode(self):
        for rate in (None, 1.0):
            with self.subTest(rate=rate):
                self.source.role_grants["H165"] = {
                    "pre_grants": ["Knowledge"],
                    "grants": [{
                        "id": "Knowledge", "entry": True, "phase": "pre",
                        "advisory": False, "rate": rate,
                    }],
                }
                valid, warnings = self.validate("H165")
                self.assertFalse(valid)
                self.assertIn("Knowledge", warnings[0])
                self.assertEqual(self.validate("H165", ("Knowledge",)), (True, []))

    def test_legacy_text_fallback_cannot_reject_screenshot(self):
        with patch.object(battle_parser, "get_character_skill_info",
                          return_value={"has_entry_buff": True}):
            self.assertEqual(self.validate("H183", source="manual"), (True, []))
