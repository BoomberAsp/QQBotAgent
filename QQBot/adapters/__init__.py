"""Platform entry adapters.

Each adapter converts one platform's events into agent.inbound views
(stage 2: inbound only) and — from stage 3 on — implements the outbound
Sender contract for that platform.

  - onebot.py       OneBot V11 / NapCat  (current production entry)
  - qq_official.py  QQ official bot API  (stage 4)
"""
