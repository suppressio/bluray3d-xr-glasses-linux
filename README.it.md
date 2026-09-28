[🇬🇧 English](README.md) | 🇮🇹 Italiano

# Blu-ray 3D sugli occhiali XR, da Linux
#### _Guarda i tuoi Blu-ray 3D sugli occhiali XR (VITURE & co.) in 3D vero: il PC Linux decodifica il 3D al volo e lo serve in rete locale come video affiancato. Nessuna conversione, nessuno spazio su disco in più._

> 🎯 **Obiettivo attuale:** hai già dei **rip MKV di Blu-ray 3D** (MakeMKV conserva il 3D MVC) e vuoi guardarli in 3D **senza convertirli**. Se hai solo il disco e non ti importa convertire, gli strumenti esistenti copiano già direttamente in SBS. **Prossimo obiettivo: riprodurre il disco stesso**, senza nessun rip: vedi la [roadmap](ROADMAP.it.md).

***Nota:*** _Provato con VITURE Pro XR + VITURE Pro Neckband e il suo **3D Player** ufficiale. Qualsiasi player capace di aprire video da una cartella di rete (SMB) e di mostrare il 3D affiancato dovrebbe funzionare allo stesso modo._

---

## Il problema

Un Blu-ray 3D non contiene due video affiancati. Il 3D è registrato in **MVC** (H.264 _Multiview Video Coding_): un normale video 2D per l'occhio sinistro più una "vista dipendente" con le differenze per l'occhio destro.

- Gli occhiali XR, e quasi tutti i player 3D, vogliono l'**SBS**, _side-by-side_: i due occhi affiancati nello stesso fotogramma.
- In pratica **l'MVC non lo decodifica quasi nessuno**, a parte i lettori Blu-ray dedicati. Android e i suoi player non ci riescono, e FFmpeg scarta in silenzio la vista dipendente: il risultato è 2D.
- La soluzione solita è **convertire** ogni film in SBS: ore di codifica e 10-20 GB in più per film.

## L'idea

Il PC decodifica l'MVC **mentre guardi** e manda il risultato agli occhiali come se fosse un normalissimo file SBS.

```
Blu-ray 3D ──MakeMKV──► film.mkv (MVC, il 3D è ancora lì)
                              │
                              ▼   PC Linux, mentre guardi
   ffmpeg (demux) ─► edge264 (MVC → SBS 3840×1080) ─► encoder (NVENC o x264) + audio
                              │
                              ▼
   "film - 3D SBS.ts"  file virtuale in una cartella condivisa SMB (su disco non esiste)
                              │   Wi-Fi
                              ▼
   Occhiali: 3D Player ► Rete locale ► 3D ► film   → 3D automatico, pausa, seek
```

- **Non si scrive niente su disco.** Il file `.ts` risulta di circa 22 GB ma non occupa spazio: ogni pezzo viene decodificato nel momento in cui il player lo legge.
- **Il seek funziona.** Il file ha bitrate costante, quindi ogni byte corrisponde a un secondo preciso del film. Quando il player salta, il PC riparte a decodificare da lì (1-2 secondi).
- **Gli occhiali vedono un file normale.** Niente app particolari né protocolli di streaming: interfaccia, riconoscimento del 3D, pausa e seek sono quelli del player.

Tutte le procedure descritte sono un compromesso ragionato tra il "manuale" e il guidato. È uno dei modi possibili per farlo.

## Requisiti

#### Hardware
- **Un lettore Blu-ray** in grado di leggere i tuoi dischi (serve solo per il rip; va bene qualsiasi lettore BD supportato da MakeMKV).
- **Un PC Linux** sulla stessa rete degli occhiali. Decodificare l'MVC è lavoro per la CPU: provato su un Ryzen 9 5900X (decodifica a circa 9 volte il tempo reale). Non ho provato CPU meno potenti.
- **Facoltativa: una GPU NVIDIA**, per codificare con NVENC. Senza, il video viene codificato dalla CPU con x264 (sul 5900X comunque circa 6 volte il tempo reale).
- **Occhiali XR + un player** che apra video da una cartella di rete SMB e riproduca il 3D SBS. Provato: VITURE Pro XR + Pro Neckband, 3D Player ufficiale.
- **Un buon Wi-Fi** (consigliati i 5 GHz): il flusso è di 24 Mbit/s.

#### Software sul PC
- [MakeMKV](https://www.makemkv.com/): copia il Blu-ray in un MKV **conservando il 3D (MVC)**. Esiste la versione Linux (gratuita finché è in beta).
- **Docker** (strada A) _oppure_ un sistema **Debian/Ubuntu** (strada B). Tutto il resto lo installa il progetto:
  - [edge264-mvc](https://github.com/jens-duttke/edge264-mvc): l'unico decoder open source della vista dipendente MVC;
  - FFmpeg, Samba (la cartella di rete), FUSE + pyfuse3 (il file virtuale).

---

## Passo 1 — Copiare il Blu-ray in MKV

- [ ] Apri il disco con **MakeMKV** e salva il titolo principale in MKV.

Con le impostazioni predefinite MakeMKV conserva i dati del 3D (MVC) dentro l'MKV. Non serve fare niente di particolare: all'avvio il programma controlla ogni MKV e **salta quelli senza 3D**, scrivendolo nel log.

> ⚖️ Copiare dischi di tua proprietà per uso personale è legale in alcuni paesi e in altri no: verifica le regole del tuo.

Metti gli MKV 3D in una cartella, per esempio `~/Video/3D`. Le sottocartelle vanno bene.

---

## Passo 2 — Installazione: scegli la strada

| | **A. Docker** | **B. Script nativo** |
|---|---|---|
| Per chi | usa già Docker | Debian / Ubuntu |
| Tocca il sistema | no: Samba, FUSE e librerie stanno nel container | sì: pacchetti, `/opt`, `smb.conf`, `fuse.conf` (si toglie tutto con `uninstall.sh`) |
| Codifica NVIDIA | serve il NVIDIA Container Toolkit | funziona subito |
| Samba già installato sul PC | conflitto sulla porta 445: va fermato | la condivisione si aggiunge alle tue |

Per prima cosa scarica il progetto:
```console
git clone https://github.com/suppressio/bluray3d-xr-glasses-linux.git
cd bluray3d-xr-glasses-linux
```

### Strada A — Docker

- [ ] Crea le impostazioni:
```console
cp .env.example .env
```
- [ ] Modifica `.env`: imposta `MOVIES_DIR` sulla cartella dei film e `AUDIO_LANG` sulla tua lingua (`ita`, `eng`, `deu`, `fra`...).
- [ ] Costruisci e avvia. La prima volta ci vuole qualche minuto, perché compila edge264 per la tua CPU:
```console
docker compose up -d --build
```
- [ ] Controlla che abbia trovato i film:
```console
docker compose logs
```
```
video encoder: x264
Tron- Legacy 3D_t04 - 3D SBS.ts  (125 min, audio #4 ita dts Surround 5.1)
mounted on /srv/bd3d — Ctrl+C to unmount
```

Per fermarlo: `docker compose down`.

##### GPU NVIDIA (facoltativo)
Installa il [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html), poi avvia con entrambi i file compose:
```console
docker compose -f docker-compose.yml -f docker-compose.nvidia.yml up -d --build
```
Nel log deve comparire `video encoder: nvenc`.
> ⚠️ Questa variante non l'ho ancora provata (sul mio PC non c'è il Container Toolkit). NVENC è provato con la strada nativa.

##### 💩 happens: porta 445 già occupata
Se sul PC gira già Samba, il container non può prendere la porta 445. I player di telefoni e occhiali di solito parlano solo con la 445. Quindi mentre guardi il film ferma il Samba del sistema (`sudo systemctl stop smbd`), oppure usa la strada B, che aggiunge una condivisione al Samba che hai già.

### Strada B — Script nativo (Debian / Ubuntu)

- [ ] Lancia l'installazione come utente normale (chiede `sudo` quando serve):
```console
./scripts/install.sh
```
Installa i pacchetti, compila edge264, installa il comando `bluray3d-xr` e aggiunge a Samba la condivisione `[3D]`, in sola lettura e ad accesso ospite. L'intestazione di [`scripts/install.sh`](scripts/install.sh) elenca tutte le modifiche che fa.

- [ ] Se `ufw` è attivo, lo script stampa il comando per aprire la condivisione alla tua rete locale, per esempio:
```console
sudo ufw allow from 192.168.1.0/24 to any port 445 proto tcp
```
- [ ] Avvialo quando vuoi guardare qualcosa:
```console
bluray3d-xr --audio-lang ita ~/Video/3D
```
Si ferma con `Ctrl+C`. Per togliere tutto: `./scripts/uninstall.sh`.

Lo script è provato in container puliti Debian 13 (trixie) e Ubuntu 24.04; il programma gira sul mio PC con Debian testing.

---

## Passo 3 — Guardarlo sugli occhiali

##### Dal Neckband VITURE:
- [ ] Apri il **3D Player**, vai nella scheda **Rete locale** e aggiungi il PC: il suo indirizzo IP (sul PC lo trovi con `hostname -I`), accesso ospite / anonimo.
- [ ] Apri la cartella **3D**: ci sono tutti i film, come `<film> - 3D SBS.ts`.
- [ ] Aprilo. Il player riconosce il formato affiancato e passa in 3D da solo. 🎉

La prima apertura e ogni salto con la barra richiedono 1-2 secondi: è il PC che riparte a decodificare dal nuovo punto.

**ATTENZIONE!** In alcuni film una parte delle scene è in 2D per scelta (in _Tron: Legacy_ le parti nel "mondo reale"). Lì i due occhi ricevono la stessa immagine: non è un errore.

##### Altri occhiali e altri player
Qualsiasi cosa apra video da una cartella SMB e mostri il 3D SBS dovrebbe andare. Qualche nota dalle mie prove sul Neckband:
- **VLC** lo riproduce, ma bisogna uscire dall'interfaccia SpaceWalker e passare alla modalità Android, avviare il video e _solo dopo_ mettere gli occhiali in modalità 3D. Funziona ma è scomodo, e a volte gli occhiali sono rimasti bloccati in modalità 3D (ho dovuto staccare il cavo).
- **XPlayer2** sul mio Neckband non ha funzionato, con nessun video.
- Il **3D Player non apre l'MKV originale**: non parte proprio. È il motivo per cui esiste questo progetto.

---

## Opzioni

| Opzione | `.env` (Docker) | Riga di comando (nativo) | Predefinito |
|---|---|---|---|
| Cartella/e dei film | `MOVIES_DIR` | argomenti posizionali | — |
| Lingue audio | `AUDIO_LANG` | `--audio-lang ita,eng` | prima traccia |
| Un file per lingua, uno con tutte, o entrambi | `AUDIO_FILES` | `--audio-files per-language\|single\|both` | `per-language` |
| Encoder video | `ENCODER` | `--encoder auto\|nvenc\|x264` | `auto` (NVENC se c'è) |
| Punto di montaggio | — | `--mount` | `/srv/bd3d` |
| Log della pipeline | — | `--log-file` | `/tmp/bd3d-pipeline.log` |

#### Lingue audio e sottotitoli
- **Più lingue**: con `--audio-lang ita,eng` ogni lingua diventa **un file a sé**
  (`ITA - film - 3D SBS.ts`, `ENG - film - 3D SBS.ts`; la lingua è all'inizio perché i player tagliano i nomi lunghi). Il 3D Player VITURE non ha un menu per le
  tracce audio e ne sceglie una da solo, per questo è il comportamento predefinito. Se il
  tuo player il menu ce l'ha, `--audio-files single` mette tutte le lingue in un file
  solo. `both` offre le due cose insieme: i file per lingua, più quello con tutte le
  lingue in una cartella `Multi-audio/`. È utile con più dispositivi; i file sono
  virtuali, quindi quelli in più non costano nulla.
- **Sottotitoli**: metti i file dei sottotitoli accanto all'MKV con lo stesso nome
  (`film.srt`, `film.ita.srt`, anche `.ass`, `.sup`…). Compaiono accanto a ogni video
  virtuale con il nome abbinato, e il player li carica come sottotitoli esterni. Se si
  vedano bene in 3D dipende dal player: il 3D Player VITURE li carica.

Formato in uscita: H.264 Full-SBS 3840×1080 a 20 Mbit/s CBR, audio AAC stereo 192 kbit/s per lingua, in un MPEG-TS a 24 Mbit/s. L'audio è convertito in stereo (DTS e TrueHD non sono supportati dalla maggior parte dei player mobili).

---

## Risoluzione dei problemi

- **Manca un film e il log dice `skipped (no MVC 3D video)`**: in quell'MKV non ci sono dati 3D. È un rip 2D, oppure il rip ha perso il flusso MVC: rifallo con MakeMKV.
- **Gli occhiali non vedono la cartella condivisa**: verifica che PC e occhiali siano sulla stessa rete, e controlla il firewall (porta 445/TCP). Da un altro PC Linux: `smbclient -N -L //<ip-del-pc>`.
- **L'immagine va a scatti**: controlla prima il Wi-Fi (5 GHz, vicino al router). Poi il log dell'ultima pipeline (`/tmp/bd3d-pipeline.log`, oppure `docker compose logs`).
- **Un film nuovo non compare**: la cartella viene letta all'avvio. Riavvia il programma o il container.

---

## Come funziona (per i curiosi)

- **edge264-mvc** decodifica entrambe le viste del flusso MVC e le scrive affiancate (`edge264_test -Ok`) come fotogrammi grezzi. FFmpeg da solo non ci riesce: scarta la vista dipendente.
- L'encoder lavora a **bitrate costante** e il muxer MPEG-TS riempie esattamente fino a 24 Mbit/s (`-muxrate`). Così il byte _X_ del file virtuale è il secondo _X_ / 3.000.000 del film.
- Un piccolo file system **FUSE** (`src/bd3d_fs.py`) espone i file. Le letture consecutive proseguono dalla pipeline in corsa, che si mette in pausa da sola se va troppo avanti rispetto al player. Un salto riavvia la pipeline dal keyframe precedente. I primi e gli ultimi 8 MB restano in cache, perché i player li rileggono per intestazioni e durata.
- **Dettaglio sul sincronismo audio/video**: quando cerca un punto in formati con B-frame, FFmpeg arretra il punto di partenza di 3/23 s. Chiedendo esattamente il keyframe _K_ finisce sul keyframe precedente, e l'immagine resta circa 1 s indietro rispetto all'audio. Per questo al video si chiede _K_ + 0,2 s e all'audio esattamente _K_ (vedi `src/pipeline.py`). Scarto misurato: 1 ms.

---

## E Windows?

Questo progetto è solo per Linux. Su Windows puoi guardare [**SyLC**](https://github.com/5ymph0en1x/SyLC), un player open source che riproduce direttamente l'MVC dei Blu-ray 3D (MKV, ISO, BDMV) e può produrre SBS. Non l'ho provato.

## Riconoscimenti e riferimenti

- [edge264-mvc](https://github.com/jens-duttke/edge264-mvc) (BSD), fork di [edge264](https://github.com/tvlabs/edge264): il decoder MVC che rende possibile tutto questo;
- [FFmpeg](https://ffmpeg.org/), [Samba](https://www.samba.org/), [pyfuse3](https://github.com/libfuse/pyfuse3), [MakeMKV](https://www.makemkv.com/);
- [Play 3D Blu-ray in SBS directly from the disc…](https://cybereality.com/play-3d-blu-ray-in-sbs-directly-from-the-disc-for-playback-on-3d-monitors-xr-glasses-and-vr-headsets-using-free-and-open-source-tools/) (cybereality): stesso obiettivo, affrontato in un altro modo.
