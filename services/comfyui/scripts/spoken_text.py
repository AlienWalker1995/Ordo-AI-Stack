"""Turn written dialogue into the words a speaker says: numbers, money, percentages and years
spelled out ("$21 billion" -> "twenty-one billion dollars", "4%" -> "four percent",
"2029" -> "twenty twenty-nine").

Used twice by the dialogue reel renderer:
- on each line before it goes to the TTS model, which garbles digits and symbols more often
  than words (the 2026-09-17 voice audit);
- on both sides of the per-line transcript check, so "6" heard against "six" intended, or
  "$300,000,000" against "300 million dollars", is not counted as a missing word.

Standard library only: the ComfyUI image has no num2words.
"""
from __future__ import annotations

import re

ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
        "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
        "eighteen", "nineteen"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
SCALES = [(10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (1000, "thousand")]
MAGNITUDE_WORDS = {"k": "thousand", "thousand": "thousand", "m": "million", "mn": "million",
                   "million": "million", "b": "billion", "bn": "billion", "billion": "billion",
                   "t": "trillion", "tn": "trillion", "trillion": "trillion"}
ORDINALS = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth",
            "nine": "ninth", "twelve": "twelfth"}


def say_integer(n: int) -> str:
    if n < 0:
        return "minus " + say_integer(-n)
    if n < 20:
        return ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return TENS[tens] + ("-" + ONES[ones] if ones else "")
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        return ONES[hundreds] + " hundred" + (" " + say_integer(rest) if rest else "")
    for value, name in SCALES:
        if n >= value:
            head, rest = divmod(n, value)
            return say_integer(head) + " " + name + (" " + say_integer(rest) if rest else "")
    raise AssertionError("unreachable")


def say_number(text: str) -> str:
    """'21' -> 'twenty-one', '4.5' -> 'four point five', '900,000' -> 'nine hundred thousand'."""
    whole, _, fraction = text.replace(",", "").partition(".")
    spoken = say_integer(int(whole or "0"))
    if fraction:
        spoken += " point " + " ".join(ONES[int(d)] for d in fraction)
    return spoken


def say_year(year: int) -> str:
    """'2029' -> 'twenty twenty-nine', '2005' -> 'two thousand five', '1999' -> 'nineteen ninety-nine'."""
    if 2000 <= year <= 2009:
        return say_integer(year)
    head, tail = divmod(year, 100)
    if tail == 0:
        return say_integer(head) + " hundred"
    return say_integer(head) + " " + (say_integer(tail) if tail >= 10 else "oh " + ONES[tail])


def say_ordinal(n: int) -> str:
    words = say_integer(n).split(" ")
    last_parts = words[-1].split("-")
    last = last_parts[-1]
    if last in ORDINALS:
        last = ORDINALS[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last += "th"
    words[-1] = "-".join(last_parts[:-1] + [last])
    return " ".join(words)


NUMBER = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
MAGNITUDE = r"(?:\s*(?:trillion|billion|million|thousand|tn|bn|mn|[kmbt])\b)?"

_MONEY = re.compile(r"([$€£])(" + NUMBER + r")(" + MAGNITUDE + r")", re.IGNORECASE)
_PERCENT = re.compile(r"(" + NUMBER + r")\s?%")
_MAGNITUDE_SUFFIX = re.compile(r"\b(" + NUMBER + r")(k|m|mn|b|bn|t|tn)\b", re.IGNORECASE)
_MULTIPLIER = re.compile(r"\b(" + NUMBER + r")x\b", re.IGNORECASE)
_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(1[89]\d\d|20\d\d)\b(?!,\d)")
_PLAIN = re.compile(r"(?<![\w.])(" + NUMBER + r")(?![\w])")
CURRENCY_WORDS = {"$": "dollars", "€": "euros", "£": "pounds"}


# Words that can follow an amount without the amount describing them ("$4 billion now",
# "$11 million to sell", "the $70 billion everyone's quoting"). Any other lowercase word after
# an amount is the thing it describes ("a $6,000 laptop", "a $3.1 billion valuation"), which is
# said with the singular unit: "six thousand dollar laptop".
NOT_DESCRIBED = frozenset("""
a an the and or but nor so yet for of in on at to from by with into onto over under per than as
is was are were be been being has have had do does did will would can could should may might must
it its it's this that these those he she they we you i me him her them us his hers their our your my
everyone everyone's everybody nobody someone anyone people
now already just still even only also too more less back total annually ago later last next each
every again away apiece upfront up down off out here there then when where while if because
before after since until during against between through without within about around per-year
""".split())
_NEXT_WORD = re.compile(r"\s+([a-z][a-z'’-]*)")


def _money_with_next_word(text: str) -> str:
    def replace(match: re.Match) -> str:
        symbol, amount, magnitude = match.group(1), match.group(2), match.group(3).strip().lower()
        spoken_amount = say_number(amount)
        if magnitude:
            spoken_amount += " " + MAGNITUDE_WORDS[magnitude]
        unit = CURRENCY_WORDS[symbol]
        next_word = _NEXT_WORD.match(text, match.end())
        describes_next = next_word is not None and next_word.group(1) not in NOT_DESCRIBED
        if describes_next or (amount.replace(",", "") == "1" and not magnitude):
            unit = unit[:-1]
        return f"{spoken_amount} {unit}"
    return _MONEY.sub(replace, text)


_RANGE = re.compile(r"(?<![\w.-])(" + NUMBER + r")-(" + NUMBER + r")(?=\s?(?:%|x\b|k\b|m\b|b\b|\s+\w))",
                    re.IGNORECASE)
_ROUND_THE_CLOCK = re.compile(r"\b24/7\b")


def spoken(text: str) -> str:
    """The line as it should be read aloud. Words are untouched; only numerals and the
    symbols attached to them change."""
    text = _ROUND_THE_CLOCK.sub("twenty-four seven", text)
    text = _RANGE.sub(lambda m: m.group(1) + " to " + m.group(2), text)
    text = _money_with_next_word(text)
    text = _PERCENT.sub(lambda m: say_number(m.group(1)) + " percent", text)
    text = _MAGNITUDE_SUFFIX.sub(
        lambda m: say_number(m.group(1)) + " " + MAGNITUDE_WORDS[m.group(2).lower()], text)
    text = _MULTIPLIER.sub(lambda m: say_number(m.group(1)) + " times", text)
    text = _ORDINAL.sub(lambda m: say_ordinal(int(m.group(1))), text)
    text = _YEAR.sub(lambda m: say_year(int(m.group(1))), text)
    text = _PLAIN.sub(lambda m: say_number(m.group(1)), text)
    return text
