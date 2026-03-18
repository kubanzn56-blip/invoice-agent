import os
import base64
import json
import re
import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import google.generativeai as genai
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/spreadsheets",
]

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CREDENTIALS_B64 = os.getenv("CREDENTIALS_B64")
TOKEN_B64 = os.getenv("TOKEN_B64")
SHEETS_ID = os.getenv("SHEETS_ID")
FIRMA_NAZWA = os.getenv("FIRMA_NAZWA", "Firma")
AUTO_WYSLIJ = os.getenv("AUTO_WYSLIJ", "true").lower() == "true"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash-lite")


def get_service(api, wersja):
    creds = None
    if TOKEN_B64:
        token_data = json.loads(base64.b64decode(TOKEN_B64).decode())
        creds = Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=SCOPES,
        )
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if CREDENTIALS_B64:
                creds_data = json.loads(base64.b64decode(CREDENTIALS_B64).decode())
                with open("/tmp/credentials.json", "w") as f:
                    json.dump(creds_data, f)
                flow = InstalledAppFlow.from_client_secrets_file("/tmp/credentials.json", SCOPES)
            else:
                flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
            with open("token.json", "w") as token:
                token.write(creds.to_json())

    return build(api, wersja, credentials=creds)


def wyciagnij_email(adres):
    match = re.search(r'<(.+?)>', adres)
    if match:
        return match.group(1).strip()
    return adres.strip()


def pobierz_maile_z_pdf(gmail):
    """Pobiera nieprzeczytane maile z załącznikami PDF."""
    results = gmail.users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=20
    ).execute()

    maile = results.get("messages", [])
    faktury = []

    for mail in maile:
        msg = gmail.users().messages().get(
            userId="me", id=mail["id"], format="full"
        ).execute()

        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        nadawca = headers.get("From", "")
        temat = headers.get("Subject", "")

        def znajdz_pdfy(payload):
            pdfy = []
            if payload.get("filename", "").lower().endswith(".pdf"):
                att_id = payload.get("body", {}).get("attachmentId")
                if att_id:
                    pdfy.append({
                        "nazwa": payload["filename"],
                        "att_id": att_id,
                        "msg_id": mail["id"]
                    })
            for part in payload.get("parts", []):
                pdfy.extend(znajdz_pdfy(part))
            return pdfy

        pdfy = znajdz_pdfy(msg["payload"])

        if pdfy:
            faktury.append({
                "msg_id": mail["id"],
                "nadawca": nadawca,
                "temat": temat,
                "pdfy": pdfy
            })

    return faktury


def analizuj_fakture_pdf(gmail, att_id, msg_id, nazwa_pliku):
    """Pobiera PDF i analizuje przez Gemini Vision."""
    attachment = gmail.users().messages().attachments().get(
        userId="me", messageId=msg_id, id=att_id
    ).execute()

    pdf_data = base64.urlsafe_b64decode(attachment["data"])

    prompt = """Przeanalizuj ten dokument faktury i wyciagnij dane w formacie JSON.

Zwroc TYLKO JSON bez zadnych komentarzy ani markdown, w nastepujacej strukturze:
{
  "numer_faktury": "",
  "data_wystawienia": "",
  "data_platnosci": "",
  "sprzedawca_nazwa": "",
  "sprzedawca_nip": "",
  "nabywca_nazwa": "",
  "nabywca_nip": "",
  "pozycje": [
    {"nazwa": "", "ilosc": "", "cena_netto": "", "vat": "", "cena_brutto": ""}
  ],
  "suma_netto": "",
  "suma_vat": "",
  "suma_brutto": "",
  "waluta": "PLN",
  "metoda_platnosci": "",
  "numer_konta": "",
  "anomalie": []
}

W polu anomalie wpisz liste problemow jesli znajdziesz:
- "Brak NIP sprzedawcy"
- "Brak NIP nabywcy"
- "Brak numeru faktury"
- "Brak daty platnosci"
- "Nieprawidlowa stawka VAT"
- "Brak numeru konta bankowego"

Jesli pole jest puste wpisz pusty string. Zwroc TYLKO JSON."""

    response = model.generate_content([
        prompt,
        {"mime_type": "application/pdf", "data": base64.b64encode(pdf_data).decode()}
    ])

    tekst = response.text.strip()
    tekst = tekst.replace("```json", "").replace("```", "").strip()

    try:
        dane = json.loads(tekst)
    except:
        dane = {
            "numer_faktury": nazwa_pliku,
            "blad": "Nie udalo sie odczytac faktury",
            "anomalie": ["Blad odczytu PDF"]
        }

    dane["plik"] = nazwa_pliku
    dane["data_dodania"] = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
    return dane


def dodaj_do_sheets(sheets, dane):
    """Dodaje wiersz do Google Sheets."""
    anomalie_txt = ", ".join(dane.get("anomalie", [])) if dane.get("anomalie") else "Brak"

    wiersz = [
        dane.get("data_dodania", ""),
        dane.get("numer_faktury", ""),
        dane.get("data_wystawienia", ""),
        dane.get("data_platnosci", ""),
        dane.get("sprzedawca_nazwa", ""),
        dane.get("sprzedawca_nip", ""),
        dane.get("nabywca_nazwa", ""),
        dane.get("nabywca_nip", ""),
        dane.get("suma_netto", ""),
        dane.get("suma_vat", ""),
        dane.get("suma_brutto", ""),
        dane.get("waluta", "PLN"),
        dane.get("metoda_platnosci", ""),
        dane.get("plik", ""),
        anomalie_txt
    ]

    sheets.spreadsheets().values().append(
        spreadsheetId=SHEETS_ID,
        range="Faktury!A:O",
        valueInputOption="USER_ENTERED",
        body={"values": [wiersz]}
    ).execute()


def inicjuj_sheets(sheets):
    """Tworzy nagłówki w arkuszu jeśli nie istnieją."""
    try:
        result = sheets.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID,
            range="Faktury!A1:O1"
        ).execute()

        if not result.get("values"):
            naglowki = [[
                "Data dodania", "Nr faktury", "Data wystawienia", "Termin platnosci",
                "Sprzedawca", "NIP sprzedawcy", "Nabywca", "NIP nabywcy",
                "Netto", "VAT", "Brutto", "Waluta", "Metoda platnosci",
                "Plik zrodlowy", "Anomalie"
            ]]
            sheets.spreadsheets().values().update(
                spreadsheetId=SHEETS_ID,
                range="Faktury!A1:O1",
                valueInputOption="USER_ENTERED",
                body={"values": naglowki}
            ).execute()
    except:
        pass


def wyslij_potwierdzenie(gmail, nadawca, temat, dane_faktur):
    """Wysyła potwierdzenie przetworzenia faktury."""
    czysty_email = wyciagnij_email(nadawca)

    liczba = len(dane_faktur)
    anomalie_wszystkie = []
    for d in dane_faktur:
        anomalie_wszystkie.extend(d.get("anomalie", []))

    if anomalie_wszystkie:
        info_anomalie = f"\nUWAGA — wykryto {len(anomalie_wszystkie)} anomalii:\n" + "\n".join(f"- {a}" for a in anomalie_wszystkie)
    else:
        info_anomalie = "\nWszystkie faktury wygladaja poprawnie."

    tresc = f"""Dzien dobry,

Potwierdzamy otrzymanie i przetworzenie {liczba} faktury/faktur z wiadomosci "{temat}".

Dane zostaly automatycznie wprowadzone do arkusza Google Sheets.
{info_anomalie}

Mozesz sprawdzic arkusz tutaj:
https://docs.google.com/spreadsheets/d/{SHEETS_ID}

Pozdrawiamy,
Invoice Agent | {FIRMA_NAZWA}
bizagent.pl"""

    msg = MIMEMultipart()
    msg["To"] = czysty_email
    msg["Subject"] = f"Re: {temat} — faktury przetworzone"
    msg.attach(MIMEText(tresc, "plain", "utf-8"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    if AUTO_WYSLIJ:
        gmail.users().messages().send(
            userId="me", body={"raw": raw}
        ).execute()
        print(f"  Potwierdzenie wyslane do: {czysty_email}")


def oznacz_jako_przeczytany(gmail, msg_id):
    gmail.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def uruchom_agenta():
    print(f"\nInvoice Agent — {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}")

    gmail = get_service("gmail", "v1")
    sheets = get_service("sheets", "v4")

    inicjuj_sheets(sheets)

    faktury_maile = pobierz_maile_z_pdf(gmail)

    if not faktury_maile:
        print("  Brak nowych maili z fakturami PDF.")
        return

    print(f"  Znaleziono {len(faktury_maile)} maili z PDF.")

    for mail in faktury_maile:
        print(f"\n  Od: {mail['nadawca']}")
        print(f"  Temat: {mail['temat']}")
        print(f"  Zalaczniki PDF: {len(mail['pdfy'])}")

        dane_faktur = []

        for pdf in mail["pdfy"]:
            print(f"    Analizuje: {pdf['nazwa']}...")
            dane = analizuj_fakture_pdf(gmail, pdf["att_id"], pdf["msg_id"], pdf["nazwa"])

            print(f"    Nr faktury: {dane.get('numer_faktury', '?')}")
            print(f"    Kwota brutto: {dane.get('suma_brutto', '?')}")

            if dane.get("anomalie"):
                print(f"    ANOMALIE: {', '.join(dane['anomalie'])}")

            dodaj_do_sheets(sheets, dane)
            dane_faktur.append(dane)

        wyslij_potwierdzenie(gmail, mail["nadawca"], mail["temat"], dane_faktur)
        oznacz_jako_przeczytany(gmail, mail["msg_id"])

    print("\nGotowe!")


if __name__ == "__main__":
    uruchom_agenta()
