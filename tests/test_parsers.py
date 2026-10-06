import io
from datetime import datetime

import pytest
from openpyxl import Workbook

from payverify.services.sms_parser import is_trusted_sender, parse_bank_sms
from payverify.services.statement import StatementError, parse_statement, read_rows
from payverify.utils import format_inr, parse_amount


@pytest.mark.parametrize("text, amount, utr", [
    # HDFC
    ("Money Received - INR 500.00 in your HDFC Bank A/c xx1234 on 05-10-2026 from VPA xyz@ybl (UPI 627812345678)",
     50000, "627812345678"),
    ("Rs.1,250.50 credited to a/c XXXXXX1234 on 05-10-26 by a/c linked to VPA 9876543210@ybl (UPI Ref No 627812345679).",
     125050, "627812345679"),
    # SBI
    ("Dear SBI UPI User, ur A/cX1234 credited by Rs500 on 05Oct26 by (Ref no 627812345670)", 50000, "627812345670"),
    # ICICI (balance must not be taken as the amount)
    ("ICICI Bank Account XX123 credited:Rs. 800.00 on 05-Oct-26. Info UPI-627812345671-RAVI. Available Balance is Rs. 10,000.00.",
     80000, "627812345671"),
    # Axis
    ("INR 1500.00 credited to A/c no. XX1234 on 05-10-26 at 10:30:00 IST. Info- UPI/P2A/627812345672/RAVI KUMAR/Axis Bank",
     150000, "627812345672"),
    # Kotak, sender UPI ID made of a 12-digit phone number must not be mistaken for the UTR
    ("Received Rs.500.00 in your Kotak Bank A/c X1234 from 919876543210@paytm on 05-10-26.UPI Ref:627812345673.",
     50000, "627812345673"),
])
def test_bank_credit_sms_formats(text, amount, utr):
    parsed = parse_bank_sms(text)
    assert parsed is not None
    assert parsed.amount_paise == amount
    assert parsed.utr == utr


@pytest.mark.parametrize("text", [
    "Rs.500.00 debited from a/c XX1234 on 05-10-26 to VPA shop@ybl (UPI Ref No 627812345678).",
    "INR 500 sent to RAVI from your A/c XX1234. UPI Ref 627812345678",
    "123456 is your OTP for a payment of Rs 500. Do not share it.",
    "Ravi has requested Rs 500 from you on Google Pay.",
    "Your salary of Rs 50,000 will be credited on 31st.",
    "Hello! Are we still meeting today?",
])
def test_non_credit_messages_ignored(text):
    assert parse_bank_sms(text) is None


def test_sms_date_and_time():
    parsed = parse_bank_sms("INR 500.00 credited to A/c XX1234 on 05-10-26 at 10:30:00 IST. UPI/P2A/627812345672/X")
    assert parsed.credited_at == datetime(2026, 10, 5, 10, 30)


def test_trusted_sender_rules():
    assert is_trusted_sender("AX-HDFCBK", [])
    assert is_trusted_sender("JD-SBIUPI-S", [])
    assert not is_trusted_sender("+919876543210", [])
    assert not is_trusted_sender("98765 43210", [])
    assert not is_trusted_sender("", [])
    assert is_trusted_sender("VM-HDFCBK", ["HDFCBK"])
    assert not is_trusted_sender("VM-ICICIB", ["HDFCBK"])


def test_amount_helpers():
    assert parse_amount("₹1,250.50") == 125050
    assert parse_amount("Rs.500/-") == 50000
    assert parse_amount(1250.5) == 125050
    assert parse_amount("0") is None
    assert parse_amount("abc") is None
    assert format_inr(125000000) == "₹12,50,000"
    assert format_inr(125050) == "₹1,250.50"
    assert format_inr(None) == "—"


def _rows_csv(text):
    return read_rows("statement.csv", text.encode())


def test_statement_sbi_layout():
    rows = _rows_csv(
        "Account Statement,,,,,,\n"
        "Txn Date,Value Date,Description,Ref No./Cheque No.,Debit,Credit,Balance\n"
        "5 Oct 2026,5 Oct 2026,BY TRANSFER-UPI/CR/627812345674/RAVI/SBIN/ravi@oksbi/UPI,TRANSFER FROM 1234,,\"1,500.00\",\"11,500.00\"\n"
        "5 Oct 2026,5 Oct 2026,TO TRANSFER-UPI/DR/627812345675/SHOP,TRANSFER TO 1234,200.00,,\"11,300.00\"\n"
        "6 Oct 2026,6 Oct 2026,CASH DEPOSIT,,,1000.00,12300.00\n"
    )
    credits, skipped = parse_statement(rows)
    assert [(c.amount_paise, c.utr) for c in credits] == [(150000, "627812345674")]
    assert credits[0].credited_at == datetime(2026, 10, 5)
    assert skipped == 1  # the cash deposit has no UPI reference


def test_statement_amount_and_type_columns():
    rows = _rows_csv(
        "Tran Date;Particulars;Amount;Dr/Cr\n"
        "05-10-2026;UPI/P2A/627812345676/RAVI;500.00;CR\n"
        "05-10-2026;UPI/P2M/627812345677/SHOP;90.00;DR\n"
    )
    credits, _ = parse_statement(rows)
    assert [(c.amount_paise, c.utr) for c in credits] == [(50000, "627812345676")]


def test_statement_xlsx():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Transaction Date", "Transaction Remarks", "Withdrawal Amount (INR )", "Deposit Amount (INR )", "Balance (INR )"])
    sheet.append([datetime(2026, 10, 5, 9, 15), "UPI/627812345678/RAVI/ravi@okaxis", None, 500, 9500])
    buffer = io.BytesIO()
    workbook.save(buffer)
    credits, _ = parse_statement(read_rows("statement.xlsx", buffer.getvalue()))
    assert [(c.amount_paise, c.utr, c.credited_at) for c in credits] == [(50000, "627812345678", datetime(2026, 10, 5, 9, 15))]


def test_statement_errors():
    with pytest.raises(StatementError):
        read_rows("statement.xls", b"old excel")
    with pytest.raises(StatementError):
        parse_statement(_rows_csv("Name,Phone\nRavi,123\n"))
