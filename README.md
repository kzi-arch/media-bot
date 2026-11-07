# Cookies

File di folder ini **tidak masuk Git** — berisi sesi login platform dan bersifat
seperti password.

## Format

Netscape cookie format, satu file per platform:

| File | Platform |
|---|---|
| `instagram.txt` | Instagram |
| `x.txt` | X / Twitter |
| `facebook.txt` | Facebook |

## Cara mengekspor

1. Install ekstensi **Get cookies.txt LOCALLY** (Chrome/Firefox).
2. Login ke platform di browser.
3. Klik ekstensi → **Export** → simpan sebagai `cookies/<platform>.txt`.

## Izin file

Bot mengubah permission menjadi `600` saat start, tapi pastikan juga di sisi Anda:

```bash
chmod 600 cookies/*.txt