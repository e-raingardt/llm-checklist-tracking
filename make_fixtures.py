"""Generates the fixture emails under fixtures/emails/.

Run once:
    python make_fixtures.py

Fictional case: Maria Schmidt applies for a loan at Nordbank.
No real data.
"""

from email.message import EmailMessage
from pathlib import Path


OUT = Path("fixtures/emails")
BANK = "Nordbank Loan Team <loans@nordbank-demo.com>"
CLIENT = "Maria Schmidt <m.schmidt@example-demo.com>"

# Placeholder attachment. Content is irrelevant - only the filename
# matters to the pipeline.
PDF = b"%PDF-1.4\n% Demo document, not real content.\n"

MAILS = [
    # (nr, name, from, date, subject, body, attachments)
    (1, "request", BANK, "Mon, 2 Mar 2026 09:14:00 +0000",
     "Your loan application - documents required",
     """Dear Ms Schmidt,

thank you for your loan application. Before we can start the assessment,
we need the following documents from you:

- a copy of your ID card
- your payslips for the last three months
- your current rental agreement
- bank statements for the last three months

Kind regards
Nordbank Loan Team""", []),

    (2, "announcement", CLIENT, "Mon, 2 Mar 2026 18:40:00 +0000",
     "Re: Your loan application - documents required",
     """Good evening,

thank you for the list. I will scan my ID card tomorrow and send it over.
The remaining documents will follow later this week.

Best regards
Maria Schmidt""", []),

    (3, "id_card", CLIENT, "Tue, 3 Mar 2026 08:12:00 +0000",
     "Re: Your loan application - ID card",
     """Good morning,

please find attached a copy of my ID card, as promised.

Best regards
Maria Schmidt""", ["id_card_schmidt.pdf"]),

    (4, "lease", CLIENT, "Wed, 4 Mar 2026 12:05:00 +0000",
     "Re: Your loan application - rental agreement",
     """Hello,

attached is my current rental agreement. I still need to request the
payslips from my employer and will forward them once I have them.

Best regards
Maria Schmidt""", ["rental_agreement_2025.pdf"]),

    # Backward step: the bank asks for something that has already arrived.
    # The model proposes 'requested', the rule rejects it (no_progress).
    (5, "reminder", BANK, "Thu, 5 Mar 2026 10:30:00 +0000",
     "Your loan application - reminder",
     """Dear Ms Schmidt,

to complete your file we kindly ask you again for a copy of your ID card
as well as your payslips.

Kind regards
Nordbank Loan Team""", []),

    # Nothing relevant: the model should return an empty list.
    (6, "appointment", BANK, "Thu, 5 Mar 2026 15:00:00 +0000",
     "Appointment confirmation",
     """Dear Ms Schmidt,

we confirm your advisory appointment on 12 March at 2:00 pm at our
Berlin-Mitte branch.

Kind regards
Nordbank""", []),

    (7, "payslips", CLIENT, "Fri, 6 Mar 2026 09:20:00 +0000",
     "Re: Your loan application - payslips",
     """Hello,

attached are my payslips for December, January and February.
That should complete the file.

Best regards
Maria Schmidt""",
     ["payslip_12.pdf", "payslip_01.pdf", "payslip_02.pdf"]),
]


def build(nr, name, sender, date, subject, body, attachments) -> None:
    msg = EmailMessage()
    msg["Message-ID"] = f"<msg-{nr:03d}@demo.local>"
    msg["From"] = sender
    msg["To"] = CLIENT if sender == BANK else BANK
    msg["Subject"] = subject
    msg["Date"] = date
    msg.set_content(body)

    for filename in attachments:
        msg.add_attachment(PDF, maintype="application", subtype="pdf",
                           filename=filename)

    path = OUT / f"{nr:02d}_{name}.eml"
    path.write_bytes(msg.as_bytes())
    print(f"  {path}  ({len(attachments)} attachment(s))")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"Writing to {OUT.resolve()}:")
    for mail in MAILS:
        build(*mail)
    print(f"\n{len(MAILS)} fixtures created.")
