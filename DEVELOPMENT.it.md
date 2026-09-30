# Sviluppo

🌍 [English](DEVELOPMENT.md)

Come avviare il programma da un clone, dove stanno le cose nel codice, cosa controllano i test e come leggere i log. Per cosa fa il programma e come, vedi [Come funziona](README.it.md#come-funziona) nel README.

---

## Preparazione

1. Fai una volta l'installazione nativa (**Strada B** nel [README](README.it.md#strada-b--script-nativo-debian--ubuntu)), compresa la condivisione Samba. Installa anche tutto quello che serve a un clone:
   - i pacchetti di sistema (FFmpeg, libbluray, libdvdread, pyfuse3, trio);
   - il decoder edge264 in `/opt/bluray3d-xr/bin`;
   - il punto di montaggio `/srv/bd3d`;
   - `user_allow_other` in `/etc/fuse.conf`.
2. Avvia il programma dal clone invece che dalla copia installata:

   ```bash
   cd src
   PATH=/opt/bluray3d-xr/bin:$PATH python3 bd3d_fs.py --debug /dev/sr0
   ```

   La prima riga del log dà la versione, per esempio `bluray3d-xr v1.1.0-2-gabc1234`: due commit dopo la v1.1.0.
3. Fermalo con **Ctrl+C**: smonta `/srv/bd3d` e cancella i suoi file temporanei.

- **Una copia alla volta.** Quella installata e quella del clone usano lo stesso punto di montaggio. La seconda non parte, e un messaggio lo spiega.
- **Dopo un crash, o un `kill -9`,** il punto di montaggio resta occupato: `fusermount3 -u /srv/bd3d`.
- **VS Code:** apri la cartella e scegli `.venv` come interprete; lo crea `scripts/check.sh`. `.vscode/` spegne Pylint, perché i controlli sono quelli di `pyproject.toml`.

---

## Dove stanno le cose

| File | Cosa fa |
|---|---|
| `src/bd3d_fs.py` | Il file system FUSE e la riga di comando. Contiene i file virtuali, il controllo del lettore e le pipeline (avvio, salto, pausa, stop), e disegna l'immagine di caricamento. |
| `src/sources.py` | Di cosa è fatto un film: `BlurayDiscSource`, `DvdSource`, `MkvSource`. Ognuno dice come leggere il suo video da un certo istante (`video_command`) e dà audio, sottotitoli e fotogrammi chiave. |
| `src/pipeline.py` | La catena di decodifica: lettore → decoder → FFmpeg che codifica a bitrate costante. Anche la scelta dell'encoder (NVENC o x264) e la qualità. |
| `src/bluray.py` | Un piccolo collegamento ctypes a libbluray: apre un disco, una ISO o una cartella, e ne legge i file decifrati. |
| `src/bdmv.py` | I file di navigazione del Blu-ray: playlist (MPLS), informazioni sulle clip (CLPI), il salto nel `.ssif`. |
| `src/disc_reader.py` | Legge un titolo Blu-ray da un certo istante. 3D: il video per edge264 su stdout e l'audio in una FIFO. 2D: un solo flusso TS filtrato. Gira come processo a sé. |
| `src/ssif_demux.py` | Divide il flusso 3D `.ssif` in H.264 più MVC per edge264. |
| `src/dvd.py` | libdvdread tramite ctypes, i file IFO, la scelta del titolo principale o degli episodi, e il salto preciso. |
| `src/dvd_reader.py` | Legge un titolo DVD da un certo istante come flusso MPEG program per FFmpeg. Gira come processo a sé. |
| `src/langs.py` | Un solo modo di scrivere ogni lingua (ISO 639-2). |
| `loaders/` | Le animazioni di caricamento incluse nel programma. Gli script Blender che le generano sono in [bluray3d-xr-loaders](https://github.com/suppressio/bluray3d-xr-loaders). |
| `tools/bdprobe.c` | La prima sonda del progetto (fase 0 della roadmap): apre un disco con libbluray e salva l'inizio del flusso 3D. |
| `typings/pyfuse3/` | Le dichiarazioni dei tipi di pyfuse3: le sue versioni 3.3, 3.4 e 3.5 dichiarano i tipi in modo diverso. |

Il percorso di un pezzo di file, dal player al disco: il player legge dei byte da un file in `/srv/bd3d` → `bd3d_fs.py` trasforma il byte in un istante (il bitrate è costante) → se nessuna pipeline è a quell'istante, ne avvia una: il lettore della sorgente (`disc_reader.py`, `dvd_reader.py` o FFmpeg per gli MKV), poi il decoder (edge264 per il 3D, altrimenti FFmpeg), poi FFmpeg che codifica il `.ts` → i byte tornano al player.

Il codice, i commenti e i messaggi del log sono in inglese. La documentazione è in due lingue: ogni file `.md` ha il suo gemello `.it.md`, da cambiare insieme.

---

## Controlli e test

```bash
scripts/check.sh                  # quello che GitHub esegue a ogni push
scripts/check.sh --integration    # anche i test sul disco nel lettore
```

`check.sh` esegue, in quest'ordine:
- **ruff**: stile ed errori comuni, con un insieme ampio di regole in `pyproject.toml`;
- **pyright**: i tipi, in modalità stretta;
- **shellcheck**: gli script in `scripts/` e `docker/`;
- **gli unit test**.

Al primo avvio crea `.venv` con gli strumenti alle versioni fissate in `requirements-dev.txt`. Le dipendenze del programma vengono dal sistema. GitHub esegue lo stesso script su Ubuntu 24.04 (`.github/workflows/check.yml`).

### Unit test (`tests/`)

Non serve né un disco né un lettore, e durano pochi secondi.
- `tests/builders.py` scrive file MPLS, CLPI e IFO e flussi TS piccoli ma veri.
- `tests/fakes.py` sostituisce libbluray e libdvdread (`FakeBluray`, `FakeDvd`).

Un file di test per modulo: `test_bdmv.py` per `bdmv.py`, e così via. Una correzione arriva con il test che avrebbe trovato il bug.

### Test di integrazione (`tests/integration/`)

Girano sul disco nel lettore (`BD3D_TEST_DRIVE`, predefinito `/dev/sr0`) o su un MKV 3D (`BD3D_TEST_MKV=~/Video/film.mkv`). Controllano:
- **cosa c'è sul disco:** titolo, durata, lingue, sottotitoli, i file che mostrerebbe;
- **fotogrammi chiave:** dove atterra un salto, in sei punti del film;
- **ingresso del decoder:** i byte dati a edge264 o a FFmpeg al 37% del film, separati e decifrati;
- **fotogrammi 3D decodificati:** 48 fotogrammi al 50%, decodificati da edge264 (solo 3D);
- **il file virtuale:** un pezzo letto attraverso la pipeline vera, con i flussi, la dimensione dell'immagine e i tempi giusti.

La prima volta su un disco registra un riferimento in `~/.cache/bluray3d-xr/reference/`: solo impronte, niente contenuto del disco. Le volte successive il risultato deve coincidere byte per byte. Dopo una modifica che deve cambiare il risultato (un filtro nuovo, un altro ordine dei flussi), registralo di nuovo:

```bash
BD3D_UPDATE_REFERENCE=1 scripts/check.sh --integration
```

Quali test contano per quale modifica:

| Modifica in | Esegui |
|---|---|
| qualsiasi file | `scripts/check.sh` |
| `bdmv.py`, `disc_reader.py`, `ssif_demux.py`, `bluray.py` | con un Blu-ray nel lettore (3D se puoi): `--integration` |
| `dvd.py`, `dvd_reader.py` | con un DVD nel lettore: `--integration` |
| `bd3d_fs.py` (salti, pipeline) | i test, poi una visione vera sugli occhiali o con VLC, con salti avanti e indietro |
| `scripts/install.sh`, `update.sh` | un container pulito Debian 13 e Ubuntu 24.04 |

---

## Log

### Il log del programma

Va nel terminale e in `~/.local/state/bluray3d-xr/bluray3d-xr.log`, con la data (`--log` per cambiarlo; con Docker: `docker compose logs -f`). Le righe da conoscere:

| Riga | Significato |
|---|---|
| `bluray3d-xr v1.1.0` | la versione: la prima cosa da chiedere in una segnalazione |
| `video encoder: nvenc` | NVENC o x264, scelto all'avvio |
| `watching /dev/sr0: insert a disc` | un lettore è sorvegliato, anche se è vuoto |
| `/dev/sr0: disc inserted, opening it` / `disc ejected` | il lettore ha visto entrare o uscire un disco |
| `+ Blu-ray 3D/ITA - Film - 3D SBS.ts (120 min, 24.0 Mbit/s, audio …)` | è comparso un file: durata, bitrate e tracce |
| `- …` | un file è sparito (espulsione) |
| `…: pipeline from 1234.5s (requested 1236.0s, offset …)` | un player ha letto lì: la decodifica parte dal fotogramma chiave prima dell'istante chiesto |
| `…: movie ready, 0.6s after the jump (…)` | dopo un salto, quanto è durata l'animazione di caricamento |
| `…: ignoring the player's late reads for the position it left` | normale dopo un salto: per un attimo il player chiede ancora la posizione vecchia |
| `…: stopping its pipeline, … is reading the same disc` | è partito un altro file dello stesso disco: un disco, una pipeline |
| `/dev/sr0: AACS, decrypted with libaacs` | la protezione del disco e cosa l'ha aperto (`MakeMKV (libmmbd)`, oppure `not encrypted`) |
| `…: the player receives 87% of the data rate …, with 60s of movie ready ahead …` / `back to real time` | il film è pronto ma il player lo prende piano: la rete (prova `Light/`) o il player stesso; poi si riprende |
| `…: the player receives 62% …, and only 0.5s of movie is ready ahead …` | il player aspetta noi: lì è lento il disco o la decodifica |
| `…: the player waited 12s for the movie at 1:09:10 …` | una lettura ha aspettato tanto la pipeline: un punto rovinato o sporco del disco, il lettore o la CPU occupati |
| `…: no reads for 120s, stopping a pipeline` | il player si è fermato o è in pausa da tempo |
| `…: read at … failed` + traceback | un bug, o un disco illeggibile: il player riceve un errore di lettura, il programma va avanti |

`--debug` aggiunge:
- ogni salto e le letture del player nei 20 secondi dopo: quanto ha aspettato e se sembrava in pausa;
- il comando completo di ogni pipeline, da copiare e rilanciare a mano (vedi sotto);
- gli stati del lettore ignorati.

### Il log della pipeline

`--log-file` (predefinito `/tmp/bd3d-pipeline.log`) contiene l'output dei lettori, dei decoder e di FFmpeg, una sezione per pipeline che inizia con `=== data ora file: pipeline from 1234.5s ===` (oltre i 10 MB la parte vecchia passa in `.1`). È il primo posto da guardare quando l'immagine o l'audio sono sbagliati. Righe utili:
- `disc_reader: clip 00098, keyframe -0.006s, ssif offset 0`: dove è partito il lettore Blu-ray;
- `dvd_reader: title 2, VOBU at 600.040s (sector 123456)`: dove è partito il lettore DVD;
- `dvd_reader: sectors X-Y unreadable, going on from Z`: un punto rovinato, saltato;
- gli avvisi di FFmpeg: "Could not find codec parameters" all'inizio è innocuo, il flusso viene trovato un attimo dopo. Una riga `Error` no.

---

## Smontare una pipeline

Il comando di una pipeline stampato da `--debug` dà playlist, titolo e tracce. Ogni passaggio si può poi eseguire da solo, da `src/`, con `PATH=/opt/bluray3d-xr/bin:$PATH`. `ffplay` mostra il risultato; su Debian è un pacchetto a parte (`sudo apt install ffplay`).

```bash
# Blu-ray 2D: il flusso filtrato, dritto in un player
python3 disc_reader.py /dev/sr0 --playlist 00800 --start 600 --mode 2d | ffplay -

# Blu-ray 3D: le due viste decodificate affiancate
python3 disc_reader.py /dev/sr0 --playlist 00070 --start 600 | edge264_test - -Ok | ffplay -f yuv4mpegpipe -

# DVD: il titolo con la sua prima traccia audio
python3 dvd_reader.py /dev/sr0 --title 2 --start 600 --audio 0x80 | ffplay -f mpeg -
```

Con la decifratura di MakeMKV il comando comincia con `LIBAACS_PATH=libmmbd LIBBDPLUS_PATH=libmmbd`, come nella riga di `--debug`.

Un pezzo di file virtuale, come lo riceve un player:

```bash
f="/srv/bd3d/Blu-ray 3D/ITA - Film - 3D SBS.ts"
dd if="$f" bs=188 skip=5000000 count=20000 status=none > /tmp/pezzo.ts
ffprobe -hide_banner /tmp/pezzo.ts
```

---

## Release

1. Tutto nei commit, `scripts/check.sh` pulito, il controllo di GitHub verde.
2. Un tag annotato e la release: `git tag -a v1.2.0 -m v1.2.0 && git push origin v1.2.0`, poi `gh release create v1.2.0` con le note.
3. `install.sh`, `update.sh` e l'immagine Docker scrivono il tag in un file `version` accanto al programma. Gli utenti aggiornano con `scripts/update.sh`.

I numeri di versione seguono cosa cambia per chi usa il programma: una funzione nuova o un nuovo tipo di disco è una versione minore (1.1 → 1.2), solo correzioni una patch (1.1.0 → 1.1.1).
