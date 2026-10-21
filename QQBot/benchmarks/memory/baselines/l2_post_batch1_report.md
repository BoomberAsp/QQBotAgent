# L2 管线 benchmark 报告

生成: 2026-10-21 00:05:00  engine@e69f011  model=deepseek-chat

### p1_gamer_student

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户玩游戏只玩辅助位，从不玩输出位 | [preference] 游戏角色偏好 所有游戏都只玩辅助位 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户每天都会肝原神日常，一天不落 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户玩游戏不打排位，只玩休闲匹配 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=7,age=3)] 用户的猫送回老家给爸妈养了，现在养了一只叫旺财的柴犬 |
| evolving_fact | ef2 | evolved | ✅ MERGED | [medium(c=6,age=3)] 用户上个月换了 iPhone，不再使用安卓手机 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [medium(c=3,age=13)] 用户作息特别规律，每天六点早起 | [medium(c=3,age=2)] 用户开学后变成熬夜党，经常凌晨两点才睡 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ❌ LEAKED | [short] 用户在上课，觉得这节课很无聊 |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对花生过敏，且症状较严重 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户心脏不太好，医生嘱咐不要通宵 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的弟弟有哮喘，家里不能长时间养带毛的宠物 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户昨天和室友大吵了一架，后来和好了，感觉反而更了解彼此了 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户暑假和好朋友去看了人生第一场演唱会，特别难忘 |
| salient_episode | se3 | canary | 🐤 CANARY-FAIL |  |
| contradiction | ct1 | single_value | ✅ MERGED | [medium(c=7,age=2)] 用户已搬出学校宿舍，目前在校外租房住 |
| contradiction | ct2 | single_value | ❌ CONTRADICTED | [preference] 饮品偏好 不喝咖啡，嫌苦 | [medium(c=6,age=2)] 用户现在每天喝两杯美式咖啡，已经离不开咖啡 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户是会计专业的学生 | [medium(c=3,age=1)] 用户是计算机专业的学生，不是会计专业 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 7 / MEDIUM 10 / LONG 0（时钟 24），画像槽位: 兴趣 1 偏好 2
开销: 24 批次，63569 in / 2185 out（cached 44928），27.7s，API错误 0

### p2_working_mom

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [short] 用户每天唯一的独处时间是睡前看书 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户不喝奶茶，觉得太甜受不了 | [preference] 饮品偏好 不喝奶茶，觉得太甜 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户全家周末固定去爬山，已坚持两年没断过 |
| evolving_fact | ef1 | evolved | ❌ CONTRADICTED | [short] 用户有一个上幼儿园的女儿，曾因女儿演出感动落泪 | [medium(c=6,age=3)] 用户的女儿今年九月升小学，适应得还不错 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=5,age=3)] 用户换了工作，现在改坐地铁通勤 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=5,age=3)] 用户上个月从干了十年的财务工作转行做保险 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对青霉素过敏 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户怀二胎时得过妊娠糖尿病，现在很注意控糖 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的小儿子有先天性心脏病，需定期复查 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户有一个上幼儿园的女儿，曾因女儿演出感动落泪 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户结婚十周年时，丈夫偷偷准备了惊喜，用户很感动 | [short] 用户结婚已十年，丈夫在结婚十周年时准备了惊喜 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户半夜带儿子挂急诊，儿子曾烧到39.8度，所幸无大碍 |
| contradiction | ct1 | single_value | ❌ CONTRADICTED | [short] 用户是独生女 | [medium(c=4,age=2)] 用户不是独生女，有一个姐姐 |
| contradiction | ct2 | single_value | ❌ CONTRADICTED | [short] 用户家里养了两条狗 | [short] 用户上个月把家里的两条狗送人了，现在家里没有宠物 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户住在杭州 | [medium(c=3,age=1)] 用户全家去年已从杭州搬到南京居住 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 15 / MEDIUM 7 / LONG 0（时钟 24），画像槽位: 兴趣 0 偏好 1
开销: 24 批次，65193 in / 2011 out（cached 45952），26.7s，API错误 0

### p3_fitness_coach

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户吃饭必须高蛋白，鸡胸肉是主食 | [preference] 饮食偏好 必须高蛋白，鸡胸肉是主食 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户习惯早上五点起来锻炼，认为晚上锻炼影响睡眠 | [preference] 锻炼时间偏好 习惯早上五点起来练，晚上练影响睡眠 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户滴酒不沾，备赛期也不喝酒 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=7,age=3)] 用户的减脂期已结束，现在进入增肌期，保持热量盈余 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=6,age=3)] 用户膝盖已康复，深蹲重量重回150公斤 |
| evolving_fact | ef3 | evolved | ✅ MERGED | [medium(c=6,age=2)] 用户已辞去连锁健身房私教工作，自己开了私教工作室 |
| one_shot_junk | j1 | absent | ❌ LEAKED | [medium(c=3,age=8)] 用户今天腿酸得走路费劲 |
| one_shot_junk | j2 | absent | ❌ LEAKED | [medium(c=3,age=7)] 用户的蛋白粉喝完了，需要再买 |
| one_shot_junk | j3 | absent | ❌ LEAKED | [medium(c=4,age=6)] 用户小区/健身房的跑步机坏了，物业维修慢 |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户海鲜过敏，吃虾蟹会起疹子 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户年轻时做过腰椎手术，硬拉必须戴护具 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的妻子是素食主义者，家里做饭要分两套 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户练了八年，昨天第一次深蹲起两倍体重 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户当私教带了一位学员三年，该学员昨天跑完人生第一个全马 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户在省赛中获得季军，差一点进入国家队选拔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=8,age=2)] 用户现在能做负重30公斤的引体向上 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=7,age=2)] 用户已停止拳击，现在专攻力量举 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户今年三十岁 | [medium(c=3,age=1)] 用户今年三十五岁 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 7 / MEDIUM 12 / LONG 0（时钟 24），画像槽位: 兴趣 1 偏好 2
开销: 24 批次，65573 in / 2701 out（cached 44672），28.9s，API错误 0

### p4_indie_dev

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户做游戏只用 Godot 引擎，不用其他引擎 | [interest] Godot 引擎 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户效率最高的时段是深夜，习惯凌晨写代码 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户的游戏全部采用像素风美术风格，并把像素风当作自己的招牌 | [interest] 像素风游戏 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=6,age=3)] 用户的第二款游戏已在上个月上架，销量还凑合 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=7,age=3)] 用户上个月招了一名全职美术，不再需要自己画所有素材 |
| evolving_fact | ef3 | evolved | ✅ EVOLVED | [medium(c=6,age=2)] 用户为了谈发行搬回深圳，此前在老家待了三年 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户患有腱鞘炎，医生要求控制打字时长 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户曾被合伙人坑害，背负违约金并正在打官司 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户的第一款游戏亏损三万块，相当于其全部积蓄的三分之一 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户收到玩家第一条长评好评时看哭了 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户去年参加 game jam，48 小时内完成一个游戏并获得名次 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户三年前辞职做独立开发，把工牌留在工位直接走人，对此一点不后悔 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [short] 用户的音乐和美术都曾外包过 | [medium(c=3,age=2)] 用户的游戏音乐是外包的，美术以前也外包过 |
| contradiction | ct2 | single_value | ✅ EVOLVED | [medium(c=7,age=2)] 用户计划改变，正在洽谈将游戏移植到 Switch 平台 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户使用 Windows 系统进行游戏开发 | [medium(c=3,age=1)] 用户两年多前已改用 Linux 系统进行游戏开发，不再使用 Windows |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 9 / MEDIUM 9 / LONG 0（时钟 24），画像槽位: 兴趣 4 偏好 0
开销: 24 批次，64588 in / 2253 out（cached 44928），28.2s，API错误 0

### p5_acg_fan

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=5,age=9)] 用户玩 cosplay 已五年，每月至少出一套 | [interest] cosplay |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户收藏手办只买正版，拒绝盗版 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户追番从不看解说剪辑，坚持看原片 |
| evolving_fact | ef1 | evolved | ✅ MERGED | [medium(c=6,age=3)] 用户的本命从初音未来换成了崩坏：星穹铁道的流萤，初音未来仍在心中但已成为墙头 |
| evolving_fact | ef2 | evolved | ✅ MERGED | [medium(c=5,age=3)] 用户加入了 cos 社团，现在逛漫展都跟社团一起，不再一个人去 |
| evolving_fact | ef3 | evolved | ❌ CONTRADICTED | [short] 用户正在学板绘，自认画得很烂 | [medium(c=3,age=2)] 用户板绘水平已提升，现在开始接约稿赚外快 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ❌ LEAKED | [short] 用户没抢到漫展门票，感到生气 |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户眼角膜受过伤，不能戴隐形眼镜，cos 时只能用框架眼镜改造 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户对乳胶过敏，购买道具手套时要特别注意材质 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户玩 cos 的事家里人不知道，聊天时不要提及 |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户上周第一次上台参加 cos 走秀，上台时紧张腿抖，下台后感觉特别爽 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户和一位认识四年的网友第一次面基，聊到展馆关门也不觉得尴尬 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户最贵的一只手办被猫推下摔碎，心痛到失眠三天 |
| contradiction | ct1 | single_value | ❌ CONTRADICTED | [preference] 动漫偏好 只看日漫，不碰国漫 | [medium(c=7,age=2)] 用户现在也追国漫了，最近追了好几部，此前只看日漫的偏好已改变 |
| contradiction | ct2 | single_value | ❌ STALE | [medium(c=6,age=2)] 用户为了流萤氪金，已充值多个648，打破了此前从不氪金的习惯 | [preference] 游戏消费习惯 从不氪金，白嫖党 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户在上海读书 | [short] 用户早毕业回成都了，上海是上学时候的事 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 10 / MEDIUM 8 / LONG 0（时钟 24），画像槽位: 兴趣 7 偏好 2
开销: 24 批次，65549 in / 2312 out（cached 45312），26.6s，API错误 0

### p6_retired_teacher

| 类别 | fid | expect | 状态 | 证据 |
|---|---|---|---|---|
| stable_preference | sp1 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户退休后每天写一小时毛笔字，练颜体 |
| stable_preference | sp2 | in_store | ✅ FOUND | [medium(c=4,age=9)] 用户听京剧听了一辈子，最爱程派 | [interest] 京剧 |
| stable_preference | sp3 | in_store | ✅ FOUND | [medium(c=5,age=8)] 用户一点辣都不能吃，肠胃不好 |
| evolving_fact | ef1 | evolved | ✅ EVOLVED | [medium(c=7,age=3)] 用户的手机操作已熟练，现在能自己网购 |
| evolving_fact | ef2 | evolved | ✅ EVOLVED | [medium(c=6,age=3)] 用户上个月搬到省城儿子家居住，帮忙带孙女 |
| evolving_fact | ef3 | evolved | ✅ EVOLVED | [medium(c=6,age=2)] 用户已确诊高血压，开始每天吃降压药 |
| one_shot_junk | j1 | absent | ✅ ABSENT |  |
| one_shot_junk | j2 | absent | ✅ ABSENT |  |
| one_shot_junk | j3 | absent | ✅ ABSENT |  |
| re_derivable | rd1 | absent | ✅ ABSENT |  |
| re_derivable | rd2 | absent | ✅ ABSENT |  |
| re_derivable | rd3 | absent | ✅ ABSENT |  |
| critical_lowfreq | cl1 | in_store | ✅ FOUND | [short] 用户对磺胺类药物过敏，年轻时曾因此休克过一次 |
| critical_lowfreq | cl2 | in_store | ✅ FOUND | [short] 用户的老伴五年前去世，现独自一人生活 |
| critical_lowfreq | cl3 | in_store | ✅ FOUND | [short] 用户左耳戴助听器，希望别人跟他说话时用文字 | [preference] 交流方式 倾向文字交流（左耳戴助听器） |
| salient_episode | se1 | canary | 🐤 CANARY-OK | [short] 用户教了三十年书，第一批学生上月回来看望他 |
| salient_episode | se2 | canary | 🐤 CANARY-OK | [short] 用户的颜体毛笔字在县里书法比赛获得一等奖 |
| salient_episode | se3 | canary | 🐤 CANARY-OK | [short] 用户与老伴去年迎来金婚纪念，为老伴写了一幅长卷，她生前看到了 |
| contradiction | ct1 | single_value | ✅ EVOLVED | [medium(c=7,age=2)] 用户退休前是语文老师，教了四十年书 |
| contradiction | ct2 | single_value | ❌ CONTRADICTED | [preference] 社交软件偏好 不玩微信，用不惯 | [medium(c=6,age=2)] 用户现在也用微信了，还在老年大学群里天天发早安 |
| contradiction | ct3 | single_value | ❌ CONTRADICTED | [short] 用户有一个孙女，正在上初中 | [short] 用户的孙女已上小学三年级，不是初中 |
| implanted_false | if1 | absent | ✅ ABSENT |  |
| implanted_false | if2 | absent | ✅ ABSENT |  |
| implanted_false | if3 | absent | ✅ ABSENT |  |

存储: SHORT 10 / MEDIUM 8 / LONG 0（时钟 24），画像槽位: 兴趣 2 偏好 2
开销: 24 批次，64424 in / 2249 out（cached 44672），26.5s，API错误 0

## 汇总

| persona | 计分通过 | 通过率 | canary | 泄漏(junk/rd/植入) | 批次 | in+out token |
|---|---|---|---|---|---|---|
| p1_gamer_student | 17/21 | 81% | 2/3 | 1 | 24 | 65754 |
| p2_working_mom | 17/21 | 81% | 3/3 | 0 | 24 | 67204 |
| p3_fitness_coach | 17/21 | 81% | 3/3 | 3 | 24 | 68274 |
| p4_indie_dev | 20/21 | 95% | 3/3 | 0 | 24 | 66841 |
| p5_acg_fan | 16/21 | 76% | 3/3 | 1 | 24 | 67861 |
| p6_retired_teacher | 19/21 | 90% | 3/3 | 0 | 24 | 66673 |
| **合计** | **106/126** | **84%** | **17/18** | **5** | | |