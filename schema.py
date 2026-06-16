"""
AquariusProxy / ZenithProxy config schema, derived from https://wiki.2b2t.vc/Commands

Field types: bool | int | float | string | enum | list
Optional: min/max/step (numbers), options (enum), unit (display).

This is a curated baseline of the most-tuned settings. It does NOT need to be
exhaustive — the editor merges it OVER whatever is in the actual config.json, so
unknown / plugin / version-specific fields still render from the file's real values.
Keys mirror the ZenithProxy config structure (AquariusProxy shares it; module name -> field).

Stored under SCHEMA, grouped by category for the UI.
"""

SCHEMA = {
  "Connection": {
    "client.connection": {
      "_label": "Client Connection",
      "autoConnect": {"type": "bool"},
      "proxy.enabled": {"type": "bool", "label": "Proxy enabled"},
      "proxy.type": {"type": "enum", "options": ["SOCKS5", "SOCKS4", "HTTP"]},
      "proxy.host": {"type": "string"},
      "proxy.port": {"type": "int", "min": 0, "max": 65535},
      "proxy.user": {"type": "string"},
      "proxy.password": {"type": "string", "secret": True},
      "timeout": {"type": "int", "unit": "s"},
    },
    "server": {
      "_label": "Destination Server",
      "address": {"type": "string"},
      "port": {"type": "int", "min": 0, "max": 65535},
    },
  },
  "Core Modules": {
    "autoReconnect": {
      "enabled": {"type": "bool"},
      "delay": {"type": "int", "unit": "s", "min": 0, "max": 300},
      "maxAttempts": {"type": "int", "min": 0, "max": 999},
    },
    "autoEat": {
      "enabled": {"type": "bool"},
      "health": {"type": "int", "min": 0, "max": 20},
      "hunger": {"type": "int", "min": 0, "max": 20},
      "warning": {"type": "bool"},
      "allowUnsafeFood": {"type": "bool"},
      "mode": {"type": "enum", "options": ["all", "whitelist", "blacklist"]},
    },
    "autoTotem": {
      "enabled": {"type": "bool"},
      "inGame": {"type": "bool"},
      "health": {"type": "int", "min": 0, "max": 20},
      "popAlert": {"type": "bool"},
      "noTotemsAlert": {"type": "bool"},
    },
    "autoRespawn": {
      "enabled": {"type": "bool"},
      "delay": {"type": "int", "unit": "ms", "min": 0, "max": 10000},
    },
    "autoArmor": {"enabled": {"type": "bool"}},
    "autoMend": {"enabled": {"type": "bool"}},
  },
  "AFK & Anti-Kick": {
    "antiAFK": {
      "enabled": {"type": "bool"},
      "rotate": {"type": "bool"},
      "swing": {"type": "bool"},
      "walk": {"type": "bool"},
      "safeWalk": {"type": "bool"},
      "jump": {"type": "bool"},
      "sneak": {"type": "bool"},
      "walkDistance": {"type": "int", "unit": "ticks", "min": 0, "max": 100},
    },
    "antiKick": {
      "enabled": {"type": "bool"},
      "playerInactivityKickMins": {"type": "int", "unit": "min", "min": 0, "max": 120},
      "minWalkDistance": {"type": "int", "unit": "blocks", "min": 0, "max": 50},
    },
    "sessionTimeLimit": {"enabled": {"type": "bool"}},
  },
  "Combat": {
    "killAura": {
      "enabled": {"type": "bool"},
      "attackDelay": {"type": "int", "unit": "ticks", "min": 0, "max": 100},
      "tpsSync": {"type": "bool"},
      "targetPlayers": {"type": "bool"},
      "targetHostileMobs": {"type": "bool"},
      "targetNeutralMobs": {"type": "bool"},
      "targetCustom": {"type": "bool"},
      "weaponSwitch": {"type": "bool"},
      "weaponType": {"type": "enum", "options": ["any", "sword", "axe"]},
      "weaponMaterial": {"type": "enum", "options": ["any", "diamond", "netherite"]},
      "raycast": {"type": "bool"},
      "priority": {"type": "enum", "options": ["none", "nearest"]},
    },
    "spawnPatrol": {
      "enabled": {"type": "bool"},
      "maxPatrolRange": {"type": "int", "unit": "blocks", "min": 0, "max": 5000},
      "targetOnlyNakeds": {"type": "bool"},
      "targetAttackers": {"type": "bool"},
      "nether": {"type": "bool"},
    },
    "spook": {
      "enabled": {"type": "bool"},
      "mode": {"type": "enum", "options": ["visualRange", "nearest"]},
    },
  },
  "Auto Disconnect": {
    "autoDisconnect": {
      "enabled": {"type": "bool"},
      "health": {"type": "int", "min": 0, "max": 20},
      "thunder": {"type": "bool"},
      "unknownPlayer": {"type": "bool"},
      "totemPop": {"type": "bool"},
      "whilePlayerConnected": {"type": "bool"},
      "autoClientDisconnect": {"type": "bool"},
      "cancelAutoReconnect": {"type": "bool"},
    },
  },
  "Chat & Spam": {
    "spammer": {
      "enabled": {"type": "bool"},
      "whisper": {"type": "bool"},
      "whilePlayerConnected": {"type": "bool"},
      "delayTicks": {"type": "int", "unit": "ticks", "min": 0, "max": 2000},
      "randomOrder": {"type": "bool"},
      "appendRandom": {"type": "bool"},
      "messages": {"type": "list"},
    },
    "autoReply": {
      "enabled": {"type": "bool"},
      "cooldown": {"type": "int", "unit": "s", "min": 0, "max": 600},
      "message": {"type": "string"},
    },
    "extraChat": {
      "enabled": {"type": "bool"},
      "hideChat": {"type": "bool"},
      "hideWhispers": {"type": "bool"},
      "hideDeathMessages": {"type": "bool"},
      "insertClickableLinks": {"type": "bool"},
    },
    "chatRelay": {
      "enabled": {"type": "bool"},
      "channel": {"type": "string", "label": "Channel ID"},
      "connectionMessages": {"type": "bool"},
      "whispers": {"type": "bool"},
      "publicChat": {"type": "bool"},
      "deathMessages": {"type": "bool"},
      "whisperMentions": {"type": "bool"},
      "nameMentions": {"type": "bool"},
      "sendMessages": {"type": "bool"},
    },
  },
  "Visual Range": {
    "visualRange": {
      "enabled": {"type": "bool"},
      "enter": {"type": "bool"},
      "leave": {"type": "bool"},
      "logout": {"type": "bool"},
      "ignoreFriends": {"type": "bool"},
      "replayRecording": {"type": "bool"},
    },
    "stalk": {"enabled": {"type": "bool"}},
  },
  "Discord": {
    "discord": {
      "enabled": {"type": "bool"},
      "channel": {"type": "string", "label": "Channel ID"},
      "token": {"type": "string", "secret": True},
      "role": {"type": "string", "label": "Role ID"},
      "manageProfileImage": {"type": "bool"},
      "manageNickname": {"type": "bool"},
      "manageDescription": {"type": "bool"},
      "managePresence": {"type": "bool"},
      "ignoreOtherBots": {"type": "bool"},
    },
  },
  "Advanced": {
    "tickRate": {"rate": {"type": "float", "min": 0.1, "max": 5.0, "step": 0.1}},
    "actionLimiter": {
      "enabled": {"type": "bool"},
      "allowMovement": {"type": "bool"},
      "movementDistance": {"type": "int", "unit": "blocks", "min": 0, "max": 1000},
      "allowInventory": {"type": "bool"},
      "allowBlockBreaking": {"type": "bool"},
      "allowChat": {"type": "bool"},
    },
    "rateLimiter": {
      "login": {"type": "bool"},
      "packet": {"type": "bool"},
    },
  },
}


def schema_field_count():
    n = 0
    for cat in SCHEMA.values():
        for mod in cat.values():
            n += sum(1 for k in mod if not k.startswith("_"))
    return n


if __name__ == "__main__":
    import json
    print(f"categories: {len(SCHEMA)}")
    print(f"fields: {schema_field_count()}")
    # validate structure
    valid = {"bool", "int", "float", "string", "enum", "list"}
    for cat, mods in SCHEMA.items():
        for mod, fields in mods.items():
            for fk, fv in fields.items():
                if fk.startswith("_"):
                    continue
                assert fv["type"] in valid, f"{cat}/{mod}/{fk}: bad type {fv['type']}"
                if fv["type"] == "enum":
                    assert "options" in fv, f"{cat}/{mod}/{fk}: enum needs options"
    print("schema structure valid")
    print(json.dumps(SCHEMA, indent=1)[:200])
