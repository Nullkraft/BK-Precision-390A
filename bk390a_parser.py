#!/usr/bin/env python3

import json
import sys


FUNCTIONS = {
    0x3B: "voltage",
    0x3D: "microamp_current",
    0x39: "milliamp_current",
    0x3F: "amp_current",
    0x33: "resistance",
    0x35: "continuity",
    0x31: "diode",
    0x32: "frequency_rpm",
    0x36: "capacitance",
    0x34: "temperature",
    0x3E: "adp0",
    0x3C: "adp1",
    0x38: "adp2",
    0x3A: "adp3",
}


RANGES = {
    "voltage": {
        0: ("400.0mV", "mV", 1),
        1: ("4.000V", "V", 3),
        2: ("40.00V", "V", 2),
        3: ("400.0V", "V", 1),
        4: ("Top V range", "V", 0),
    },
    "microamp_current": {
        0: ("400.0uA", "uA", 1),
        1: ("4000uA", "uA", 0),
    },
    "milliamp_current": {
        0: ("40.00mA", "mA", 2),
        1: ("400.0mA", "mA", 1),
    },
    "amp_current": {
        0: ("40.00A", "A", 2),
    },
    "resistance": {
        0: ("400.0ohm", "ohm", 1),
        1: ("4.000kOhm", "kOhm", 3),
        2: ("40.00kOhm", "kOhm", 2),
        3: ("400.0kOhm", "kOhm", 1),
        4: ("4.000MOhm", "MOhm", 3),
        5: ("40.00MOhm", "MOhm", 2),
    },
    "continuity": {
        0: ("400.0ohm", "ohm", 1),
    },
    "diode": {
        0: ("4.000V", "V", 3),
    },
    "frequency": {
        0: ("4.000kHz", "kHz", 3),
        1: ("40.00kHz", "kHz", 2),
        2: ("400.0kHz", "kHz", 1),
        3: ("4.000MHz", "MHz", 3),
        4: ("40.00MHz", "MHz", 2),
        5: ("400.0MHz", "MHz", 1),
    },
    "rpm": {
        0: ("40.00kRPM", "kRPM", 2),
        1: ("400.0kRPM", "kRPM", 1),
        2: ("4.000MRPM", "MRPM", 3),
        3: ("40.00MRPM", "MRPM", 2),
        4: ("400.0MRPM", "MRPM", 1),
        5: ("4000MRPM", "MRPM", 0),
    },
    "capacitance": {
        0: ("4.000nF", "nF", 3),
        1: ("40.00nF", "nF", 2),
        2: ("400.0nF", "nF", 1),
        3: ("4.000uF", "uF", 3),
        4: ("40.00uF", "uF", 2),
        5: ("400.0uF", "uF", 1),
        6: ("4.000mF", "mF", 3),
        7: ("40.00mF", "mF", 2),
    },
    "temperature": {
        0: ("Temperature", "C", 0),
    },
}


def normalize_frame(frame):
    text = frame.rstrip("\r\n")
    if len(text) != 9:
        raise ValueError("expected 9 payload characters, got %d" % len(text))
    return text


def decode_bits(packet):
    return ord(packet) & 0x7F


def decode_digits(packets):
    digits = packets[1:5]
    if not all("0" <= digit <= "9" for digit in digits):
        raise ValueError("digit packets must be ASCII digits")
    raw = "".join(digits)
    return raw, int(raw)


def decode_status(packet):
    value = decode_bits(packet)
    return {
        "judge": bool(value & 0x08),
        "sign": bool(value & 0x04),
        "battery_low": bool(value & 0x02),
        "overflow": bool(value & 0x01),
    }


def decode_option1(packet):
    value = decode_bits(packet)
    return {
        "pmax": bool(value & 0x08),
        "pmin": bool(value & 0x04),
        "vahz": bool(value & 0x01),
    }


def decode_option2(packet):
    value = decode_bits(packet)
    return {
        "dc": bool(value & 0x08),
        "ac": bool(value & 0x04),
        "auto": bool(value & 0x02),
        "apo": bool(value & 0x01),
    }


def resolved_mode(function_name, status):
    if function_name != "frequency_rpm":
        return function_name
    return "frequency" if status["judge"] else "rpm"


def temperature_unit(status):
    return "C" if status["judge"] else "F"


def top_voltage_label(option2):
    if option2["dc"] and not option2["ac"]:
        return "1000V DC"
    if option2["ac"] and not option2["dc"]:
        return "750V AC"
    return "Top V range"


def range_info(mode, range_code, status, option2):
    info = RANGES.get(mode, {}).get(range_code)
    if info is None:
        return None

    label, unit, decimals = info
    if mode == "temperature":
        unit = temperature_unit(status)
    if mode == "voltage" and range_code == 4:
        label = top_voltage_label(option2)
    return {
        "label": label,
        "unit": unit,
        "decimals": decimals,
    }


def format_number(raw_value, decimals, negative):
    digits = str(raw_value).rjust(decimals + 1, "0")
    if decimals:
        digits = digits[:-decimals] + "." + digits[-decimals:]
    if negative:
        digits = "-" + digits
    return digits


def numeric_value(raw_value, decimals, negative):
    value = raw_value / (10 ** decimals)
    if negative:
        value = -value
    return value


def summary_suffix(mode, option2):
    if mode in {
        "voltage",
        "microamp_current",
        "milliamp_current",
        "amp_current",
    }:
        if option2["dc"] and not option2["ac"]:
            return " DC"
        if option2["ac"] and not option2["dc"]:
            return " AC"
    return ""


def parse_frame(frame):
    frame = normalize_frame(frame)
    packets = list(frame)
    raw_digits, raw_value = decode_digits(packets)
    range_code = decode_bits(packets[0]) - 0x30
    function_code = decode_bits(packets[5])
    status = decode_status(packets[6])
    option1 = decode_option1(packets[7])
    option2 = decode_option2(packets[8])
    function_name = FUNCTIONS.get(function_code, "unknown")
    mode = resolved_mode(function_name, status)
    info = range_info(mode, range_code, status, option2)

    result = {
        "frame": frame,
        "function": function_name,
        "function_code": "0x%02X" % function_code,
        "mode": mode,
        "range_code": range_code,
        "raw_digits": raw_digits,
        "status": status,
        "option1": option1,
        "option2": option2,
    }

    if info is not None:
        result["range_label"] = info["label"]
        result["unit"] = info["unit"]
        result["decimals"] = info["decimals"]

    if status["overflow"]:
        result["display"] = "OL"
        result["value"] = None
        result["summary"] = "OL" if info is None else "OL %s" % info["unit"]
        return result

    if info is None:
        result["display"] = raw_digits
        result["value"] = raw_value
        result["summary"] = raw_digits
        return result

    display = format_number(raw_value, info["decimals"], status["sign"])
    result["display"] = display
    result["value"] = numeric_value(raw_value, info["decimals"], status["sign"])
    result["summary"] = "%s %s%s" % (
        display,
        info["unit"],
        summary_suffix(mode, option2),
    )
    return result


def iter_frames(args):
    if args:
        for arg in args:
            yield arg
        return

    for line in sys.stdin:
        frame = line.strip()
        if frame:
            yield frame


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    saw_frame = False
    for frame in iter_frames(argv):
        saw_frame = True
        print(json.dumps(parse_frame(frame), indent=2, sort_keys=True))

    if saw_frame:
        return 0

    print("usage: python3 bk390a_parser.py FRAME [FRAME ...]", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
