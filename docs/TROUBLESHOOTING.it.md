🌐 [English](TROUBLESHOOTING.md) | **Italiano**

# Risoluzione dei problemi

> Guida per l'utente, aggiornata: i problemi noti con la soluzione, e come segnalarne uno nuovo.

Torna al [README](../README.it.md).

- **Il film non compare e il log dice `cannot decrypt`**: nessuna chiave funzionante per quel disco. Aggiorna il `KEYDB.cfg`, oppure installa e registra MakeMKV (strada nativa).
- **Un Blu-ray non compare, il log dice `BD+ disc: not decrypted`**: il disco ha la protezione BD+ e libbdplus non è partito: manca la sua configurazione `~/.config/bdplus/vm0/` (vedi [Decifratura](../README.it.md#decifratura-le-chiavi-le-porti-tu)). MakeMKV aprirebbe il disco, ma le sue correzioni sbagliano dopo un salto nel film, quindi per il BD+ non viene usato.
- **Un Blu-ray non compare, il log dice `BD+ disc: no conversion table`**: libbdplus parte, ma in `~/.config/bdplus/convtab/` non c'è la tabella di questo disco. Senza, libbdplus eseguirebbe da solo il programma BD+ del disco, che sui dischi più recenti fallisce e rovina l'immagine, quindi il disco viene saltato. Aggiungi gli aggiornamenti dell'archivio che contengono il tuo disco.
- **Un film con BD+ parte ma si rovina dopo un salto**: sul tuo sistema `libbdplus.so.0` potrebbe essere un link a `libmmbd` di MakeMKV (una vecchia ricetta di MakeMKV). Il programma non può accorgersene; togli il link e reinstalla libbdplus.
- **Un DVD non compare, il log dice `encrypted (CSS) and libdvdcss is missing`**: installa libdvdcss (vedi [Decifratura](../README.it.md#decifratura-le-chiavi-le-porti-tu)).
- **Chiudendo il programma mentre guardi, l'immagine resta ferma una ventina di secondi**: è il player che aspetta prima di arrendersi, perché il file è sparito (il programma si ferma in un secondo). Ferma prima il video sugli occhiali, poi il programma.
- **La riproduzione si ferma ogni pochi secondi**: il log dice se è la rete troppo lenta per quel file (allora aprilo da `Light/`) o il disco che non tiene il passo (un graffio o un'impronta: puliscilo); altro in [Rete e qualità](OPTIONS.it.md#rete-e-qualità).
- **Inserendo il disco non succede niente**: verifica che il lettore sia davvero `/dev/sr0` (`lsblk -d -o NAME,MODEL | grep sr`), che il tuo utente possa leggerlo (`ls -l /dev/sr0`, gruppo `cdrom`) e, con Docker, che il dispositivo sia passato al container.
- **Gli occhiali non vedono la cartella condivisa**: verifica che PC e occhiali siano sulla stessa rete, e controlla il firewall (porta 445/TCP). Da un altro PC Linux: `smbclient -N -L //<ip-del-pc>`.
- **L'immagine va a scatti**: controlla prima il Wi-Fi (5 GHz, vicino al router). Poi il log delle pipeline (`/tmp/bd3d-pipeline.log`, oppure `docker compose logs`).
- **Un file nuovo in `MOVIES_DIR` non compare**: le cartelle vengono lette all'avvio. Riavvia il programma o il container (i lettori invece sono controllati di continuo).
- **Dopo un salto compare per qualche secondo un'animazione di caricamento, e il film riparte un po' dopo il punto scelto**: il lettore si sta spostando nel nuovo punto. [docs/OPTIONS.it.md](OPTIONS.it.md#animazione-di-caricamento-dopo-un-salto) spiega perché, e come spegnerla.

Per segnalare un problema: [apri una issue](https://github.com/suppressio/bluray3d-xr-glasses-linux/issues/new/choose) con il modulo **Something does not work**. Chiede la versione (`bluray3d-xr --version`), il disco, il player e il log del programma intorno al problema (è salvato in `~/.local/state/bluray3d-xr/bluray3d-xr.log`); se riesci a farlo ripetere, avvia il programma con `--debug`. Non incollare mai chiavi.
