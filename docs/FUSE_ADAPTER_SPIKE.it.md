🌐 [English](FUSE_ADAPTER_SPIKE.md) | **Italiano**

# Spike sull'adapter FUSE: mfusepy contro pyfuse3 su Linux

> Spike per la [#129](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/129), issue di approccio per la [#7](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/7) (il port multipiattaforma). Eseguito su un container cloud (Debian sid, AMD Ryzen 9 5900X, 24 thread), non il target Debian 13/Ubuntu 24.04 del progetto e non hardware reale: nessun lettore ottico, nessun visore, nessun client Samba. I numeri qui sotto sono il limite di quello che si può mostrare con un mount usa-e-getta; il via libera su hardware reale è elencato in *Cosa resta da provare a mano*.

## Riepilogo

**mfusepy tiene come adapter FUSE su Linux per il port della #7**, con un vero limite (`max_read`) che non conta al bitrate di questo progetto e una domanda aperta (l'ordine dei thread) a cui la #6 deve rispondere comunque. Niente lo esclude.

| | pyfuse3 (oggi) | mfusepy 3.1.1 |
|---|---|---|
| Lettura sequenziale, un flusso | ~300 MB/s | ~310 MB/s |
| Due flussi insieme (inizio + fine del file) | ~625 MB/s aggregati, senza serializzazione | ~620 MB/s aggregati, senza serializzazione |
| La `read()` più grande che il kernel ha chiesto in una chiamata | 1 MiB | 128 KiB (l'opzione di mount `max_read=` non ha avuto effetto) |
| `direct_io` (VirtualFile.changing) | rispettato (già in produzione) | rispettato — verificato sotto |
| chi ha aperto il file (PID) | il PID di chi legge davvero, non un thread interno di FUSE | lo stesso, stesso percorso di codice (`/proc/<pid>/status`) |
| thread | event loop di trio + thread pool per lo scaricamento | loop multi-thread nativo di libfuse3 |
| nei repository Debian/Ubuntu | sì (`python3-pyfuse3`) | no (solo PyPI) |

Entrambi stanno tre ordini di grandezza sopra i ~3 MB/s che serve a un flusso 3D, quindi il throughput non è un problema in nessuno dei due casi. Le due cose da portare nel port:
- **`max_read` resta fermo a 128 KiB con mfusepy** in questa versione: l'opzione di mount `max_read=` (il modo pre-FUSE-3 di alzarlo) viene ignorata in silenzio, e `fuse_conn_info` in questa build non ha un campo `max_pages`/`max_read` da impostare via codice. Otto volte più chiamate a `read()` per gli stessi byte, ognuna che attraversa il confine ctypes — irrilevante a 3 MB/s, ma da riverificare se un codec futuro chiedesse un bitrate molto più alto.
- **Il loop multi-thread di default di mfusepy ha rimescolato l'ordine delle letture di un singolo flusso abbastanza spesso da contare**: leggere 100 MB in sequenza da un mount ha fatto riavviare la pipeline di `library.py` 32 volte (`JUMP_TOLERANCE` scattato per letture arrivate al backend in un ordine diverso da quello in cui il player le ha emesse, visibile confrontando gli offset tra i thread worker nel log delle letture), contro 2 per pyfuse3 sulla stessa lettura. `nothreads=True` (un solo worker, libfuse serializza le richieste) l'ha portato a 5 senza perdere la proprietà "due letture non si bloccano a vicenda" (due flussi/file aperti *diversi* avanzano ancora insieme; è il rimescolamento dentro lo stesso flusso a sparire). È esattamente la domanda sull'ordine a cui [#6](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/6) deve già rispondere per i thread semplici; non è scope nuovo, è solo segnalata qui perché la #6 la eredita invece di scoprirla da capo.

## Cosa è stato misurato, e come

Un Tree usa-e-getta con un `VirtualFile` (`src/library.py`, `src/tree.py`), alimentato dalla stessa pipeline finta che usa `tests/test_library.py` invece di ffmpeg vero/un disco vero (così il codice di prodotto resta intatto e i dati di base sono abbastanza veloci che il front end, non la fonte dei dati, è il collo di bottiglia). Due mount, uno con il vero `src/fuse_fs.py::Bd3dFS` non modificato e uno con una sottoclasse usa-e-getta di `mfusepy.Operations` (basata sui path, non esiste un equivalente nel prodotto), entrambi che registrano ogni chiamata a `read()` (offset, dimensione, PID di chi chiama, thread che serve, inizio/fine) in un file JSONL letto poi per i numeri sopra. Entrambi eseguiti sotto `systemd-run --user --scope -p MemoryMax=2G -p MemorySwapMax=0`; nessuno dei due si è avvicinato al limite (il buffer di base arriva al massimo intorno ai 256 MB di `AHEAD_MAX` per flusso).

Codice: `tools/fuse_adapter_spike/` (`common.py` per il Tree/VirtualFile condiviso, `serve_pyfuse3.py`, `serve_mfusepy.py`). Tenuto nel repository come sonda riproducibile, nello stesso spirito di `tools/bdprobe.c`; non collegato a `scripts/check.sh` (serve `pip install mfusepy==3.1.1`, non una dipendenza del progetto — vedi *Packaging* sotto).

Per riprodurlo:
```bash
.venv/bin/pip install mfusepy==3.1.1   # non in requirements-dev.txt: vedi Packaging
.venv/bin/python tools/fuse_adapter_spike/serve_pyfuse3.py /tmp/m1 /tmp/m1.readlog &
.venv/bin/python tools/fuse_adapter_spike/serve_mfusepy.py /tmp/m2 /tmp/m2.readlog &
dd if="/tmp/m1/Movie - 3D SBS.ts" bs=1M count=300 of=/dev/null status=progress
fusermount3 -u /tmp/m1; fusermount3 -u /tmp/m2
```

### direct_io, verificato con un prima/dopo

`VirtualFile.changing = True` vuol dire che lo stesso offset può restituire byte diversi in due letture (l'animazione di caricamento, poi il film); senza `direct_io` il kernel servirebbe la seconda lettura dalla sua cache delle pagine invece di richiamare il backend. Verificato con cinque `pread()` sullo stesso (offset, dimensione) attraverso un solo file descriptor aperto:
- con `direct_io` impostato (il percorso normale di entrambi gli adapter): **5/5** chiamate sono arrivate alla `read()` del backend, ognuna con l'offset esatto richiesto.
- con `direct_io` disattivato a forza (una prova di controllo, `vf.changing = False`): **1** sola chiamata è arrivata al backend, allineata alle pagine del kernel (`123453440`, un multiplo di 4096) e dimensionata per coprire la richiesta più il read-ahead; le altre quattro sono state servite dalla cache.

mfusepy espone questo tramite `raw_fi=True` (l'adapter imposta `fi.direct_io = 1` in `open()`, ricalcando `Bd3dFS.open()` e il suo `pyfuse3.FileInfo(direct_io=...)`) e si comporta in modo identico al percorso già in produzione di pyfuse3.

### Chi ha aperto il file

`fuse_get_context()[2]` (mfusepy) e `RequestContext.pid` (pyfuse3) danno entrambi il campo `fuse_in_header.pid` del *kernel*, che — come già documenta `_process()` in `fuse_fs.py` — è il **thread** che ha chiamato, non il suo processo; entrambi gli adapter hanno bisogno della stessa ricerca `/proc/<pid>/status` → `Tgid` che `Bd3dFS` già fa, ed entrambi hanno risolto correttamente un sottoprocesso `dd` (nello stile `pid 326435 (dd)`) in ogni prova sopra. Niente da cambiare nella logica di `_process()`, solo da dove viene chiamata.

## Packaging

`apt-cache search`/`policy` su Debian sid: nessun `python3-mfusepy`, in nessuna versione — solo PyPI (`mfusepy==3.1.1`, Python puro, un solo file, binding ctypes a libfuse, licenza ISC). Serve lo stesso pacchetto di sistema `fuse3` che la condivisione Samba già installa (`libfuse3-4`, tramite il pacchetto `fuse3` di `scripts/install.sh`) — niente di nuovo lì, cambia solo il binding Python.

Il Python di sistema di Debian è externally managed (`pip install` si rifiuta senza `--break-system-packages` o un venv), e le dipendenze di questo progetto dovrebbero venire dal sistema (`AGENTS.md`), non da pip all'installazione. Dato che mfusepy è un solo file con licenza ISC senza altre dipendenze Python, **vendorizzarlo nel repository** (vicino a `typings/pyfuse3/`, che già vendorizza stub per lo stesso motivo: attrito di packaging) sembra la scelta pratica per le issue del port, piuttosto che pip all'installazione o aspettare che Debian/Ubuntu lo impacchettino.

Togliere pyfuse3 toglie `python3-pyfuse3` (già un `Recommends` in `scripts/build-deb.sh`) **e `python3-trio`**: `grep -rln "import trio" src/` trova solo `fuse_fs.py` (`bd3d_fs.py` ha già tolto trio per i thread semplici, commit `a13747a`). Una scoperta collaterale non legata allo spike: `python3-trio` oggi è un `Depends` forte in `build-deb.sh` anche per le installazioni `--share dlna` che non toccano mai `fuse_fs.py` — già sbagliato oggi, indipendentemente da questo spike; elencato come lavoro successivo sotto invece che corretto qui (fuori scope: nessuna modifica al prodotto o al packaging in questa issue).

## Fuori scope qui

Confermato non toccato, come richiesto dall'issue: nessuna modifica a `src/`, nessun lavoro su macOS/Windows (#95, #102). Il codice usa-e-getta vive tutto sotto `tools/fuse_adapter_spike/`.

## Cosa resta da provare a mano

- Hardware reale: Debian 13/Ubuntu 24.04 (non sid), un disco vero, una vera riproduzione 3D sul visore — questo spike ha avuto solo un container cloud e un file di base sintetico.
- Ripetere le verifiche su `max_read`/ordine dei thread contro qualunque riscrittura di `src/fuse_fs.py` producano le issue della #7, dato che qui sono misurate contro un adapter usa-e-getta, non quello vero.
