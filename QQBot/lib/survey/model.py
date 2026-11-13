"""Survey schema model for the SurveyCake questionnaire flow (P0).

The survey definition is the public JSON served at
``https://www.surveycake.com/s3/json/<svid>.json``. This module only parses
it into immutable dataclasses and validates answers; it performs no I/O.
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass

TEXT_TYPES = frozenset({"TXTSHORT", "TXTLONG"})
CHOICE_TYPES = frozenset({"CHOICEONE", "CHOICEMULTI"})

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_DEADLINE_RE = re.compile(
    r"(\d{4})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})"
    r"\s+(\d{1,2}):(\d{2})"
)


def plain_text(value) -> str:
    """Strip HTML tags/entities from a source field (survey JSON ships HTML)."""
    if not value:
        return ""
    raw = _BR_RE.sub("\n", str(value))
    raw = _TAG_RE.sub("", raw)
    return html.unescape(raw).strip()


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def extract_deadline_hint(welcome_text: str) -> str:
    """Return "2026/11/23 13:59（UTC+8）" from the welcome copy, or ""."""
    text = plain_text(welcome_text)
    match = _DEADLINE_RE.search(text)
    if not match:
        return ""
    year, month, day, hour, minute = (int(x) for x in match.groups())
    timezone_hint = "（UTC+8）" if ("UTC+8" in text or "UTC+08" in text) else ""
    return f"{year:04d}/{month:02d}/{day:02d} {hour:02d}:{minute:02d}{timezone_hint}"


@dataclass(frozen=True)
class SurveyOption:
    option_id: int
    sn: int
    text: str
    follow_up_input: bool = False
    orders: int = 0


@dataclass(frozen=True)
class SurveyQuestion:
    subject_id: int
    order: int
    type: str
    text: str
    desc: str
    required: bool
    length_bounds: tuple[int, int] | None
    options: tuple[SurveyOption, ...] = ()

    @property
    def is_text(self) -> bool:
        return self.type in TEXT_TYPES

    @property
    def is_choice(self) -> bool:
        return self.type in CHOICE_TYPES

    def option(self, option_id: int | None) -> SurveyOption | None:
        if not option_id:
            return None
        return next((o for o in self.options if o.option_id == option_id), None)

    def option_number(self, option_id: int | None) -> int:
        for index, option in enumerate(self.options, 1):
            if option.option_id == option_id:
                return index
        return 0


@dataclass(frozen=True)
class SurveyDef:
    svid: str
    title: str
    status: str
    language: str
    welcome_text: str
    deadline_hint: str
    re_captcha: bool
    ppl_count: int
    questions: tuple[SurveyQuestion, ...]
    schema_hash: str

    def question(self, subject_id: int) -> SurveyQuestion | None:
        return next((q for q in self.questions if q.subject_id == subject_id), None)

    def question_by_number(self, number: int) -> SurveyQuestion | None:
        index = number - 1
        if 0 <= index < len(self.questions):
            return self.questions[index]
        return None

    def number_of(self, subject_id: int) -> int:
        for index, question in enumerate(self.questions, 1):
            if question.subject_id == subject_id:
                return index
        return 0

    @property
    def required_count(self) -> int:
        return sum(1 for q in self.questions if q.required)


@dataclass
class Answer:
    kind: str                       # "text" | "choice"
    text: str = ""
    option_id: int | None = None

    def is_empty(self) -> bool:
        if self.kind == "choice":
            return not self.option_id
        return not (self.text or "").strip()


def _schema_hash(questions: tuple[SurveyQuestion, ...]) -> str:
    parts: list[str] = []
    for question in questions:
        bounds = ""
        if question.length_bounds is not None:
            bounds = f"{question.length_bounds[0]}-{question.length_bounds[1]}"
        parts.append(
            f"{question.subject_id}|{question.type}|{int(question.required)}|{bounds}"
        )
        for option in question.options:
            parts.append(f"  {option.option_id}|{int(option.follow_up_input)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def parse_survey_json(data: dict, svid: str = "") -> SurveyDef:
    """Parse one SurveyCake ``/s3/json/<svid>.json`` payload into SurveyDef."""
    if not isinstance(data, dict):
        raise ValueError("问卷定义格式无效")
    raw_subjects = data.get("subjects") or []
    if not isinstance(raw_subjects, list):
        raise ValueError("问卷题目格式无效")

    questions: list[SurveyQuestion] = []
    for index, subject in enumerate(raw_subjects):
        if not isinstance(subject, dict):
            continue
        subject_id = _to_int(subject.get("ID"))
        if not subject_id:
            continue
        question_type = str(subject.get("type") or "").upper()
        bounds = None
        lower = subject.get("lowerbounds")
        upper = subject.get("upperbounds")
        if question_type in TEXT_TYPES and lower is not None and upper is not None:
            bounds = (_to_int(lower), _to_int(upper))

        options: list[SurveyOption] = []
        for raw_option in subject.get("options") or []:
            if not isinstance(raw_option, dict):
                continue
            option_id = _to_int(raw_option.get("ID"))
            if not option_id:
                continue
            extras = raw_option.get("extras") or {}
            options.append(SurveyOption(
                option_id=option_id,
                sn=_to_int(raw_option.get("sn")),
                text=plain_text(raw_option.get("text")),
                follow_up_input=bool(extras.get("follow_up_input")),
                orders=_to_int(raw_option.get("orders")),
            ))
        # Display order is `orders`; `sn` is not monotonic in real surveys
        # (e.g. "其他建议" has sn=5 but orders=4). Sorting by sn would make the
        # bot's "选N" buttons point at the wrong option.
        options.sort(key=lambda option: (option.orders, option.sn, option.option_id))

        questions.append(SurveyQuestion(
            subject_id=subject_id,
            order=_to_int(subject.get("orders"), index),
            type=question_type,
            text=plain_text(subject.get("text")),
            desc=plain_text(subject.get("descpt")),
            required=bool(_to_int(subject.get("required"))),
            length_bounds=bounds,
            options=tuple(options),
        ))

    questions.sort(key=lambda question: (question.order, question.subject_id))
    frozen = tuple(questions)
    welcome = str(data.get("welcometext") or "")
    captcha = str(data.get("re_captcha") or "N").strip().upper()
    return SurveyDef(
        svid=svid or str(data.get("svid") or ""),
        title=plain_text(data.get("title")) or "（未命名问卷）",
        status=str(data.get("status") or ""),
        language=str(data.get("language") or ""),
        welcome_text=welcome,
        deadline_hint=extract_deadline_hint(welcome),
        re_captcha=captcha not in ("", "N", "0", "FALSE", "NO"),
        ppl_count=_to_int(data.get("ppl_count")),
        questions=frozen,
        schema_hash=_schema_hash(frozen),
    )


def validate_answers(survey: SurveyDef, answers: dict[int, Answer]) -> list[str]:
    """Return human-readable problems with a draft's answers ([] = valid)."""
    issues: list[str] = []
    known = {question.subject_id for question in survey.questions}
    for subject_id in answers:
        if subject_id not in known:
            issues.append(f"存在未知题号：{subject_id}")

    for question in survey.questions:
        number = survey.number_of(question.subject_id)
        label = f"第 {number} 题「{question.text[:18]}」"
        answer = answers.get(question.subject_id)
        if answer is None or answer.is_empty():
            if question.required:
                issues.append(f"{label}为必填项")
            continue
        if question.is_text:
            value = (answer.text or "").strip()
            if question.length_bounds:
                low, high = question.length_bounds
                if low == high and len(value) != low:
                    issues.append(f"{label}需要恰好 {low} 个字符（当前 {len(value)}）")
                elif len(value) < low or (high and len(value) > high):
                    issues.append(
                        f"{label}长度需在 {low}-{high} 个字符之间（当前 {len(value)}）"
                    )
        elif question.is_choice:
            option = question.option(answer.option_id)
            if option is None:
                issues.append(f"{label}的选项无效，请重新选择")
            elif option.follow_up_input and not (answer.text or "").strip():
                issues.append(f"{label}选择了「{option.text}」，需要补充说明")
    return issues


def describe_answer(question: SurveyQuestion, answer: Answer | None) -> str:
    """Render one answer for the review card ("" when unanswered)."""
    if answer is None or answer.is_empty():
        return ""
    if question.is_choice:
        option = question.option(answer.option_id)
        if option is None:
            return "（无效选项）"
        text = f"{question.option_number(option.option_id)}. {option.text}"
        extra = (answer.text or "").strip()
        if option.follow_up_input and extra:
            text += f"（补充：{extra}）"
        return text
    return (answer.text or "").strip()


def mask_member_id(value: str, keep: int = 2) -> str:
    """Mask an account/member id for group-visible cards and logs."""
    value = (value or "").strip()
    if len(value) <= keep * 2:
        return "*" * len(value)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]