"""
plugins/_air_words.py — word suggestions for Air Control's air keyboard.

Helper module (leading underscore: the plugin loader skips it). Stdlib only.

WHY LOCAL AND WHY THIS SIMPLE
    Typing in the air costs a pinch per letter, so three good guesses above the
    keys save most of the work on longer words. The guesses must appear the
    moment a letter is typed: a model call per keystroke would be slow, cost
    money and send every half-typed word somewhere. A ranked list of frequent
    words plus the words this user actually types is instant, private and good
    enough for the short messages people type this way.

    Ranking = learned count x 2 + a base score from the word's place in the
    frequency list, so a word the user types often climbs above a common word
    they never use. Accents are folded for matching ("ete" finds "été"), since
    the air keyboard has no accented keys.

Learned counts live in plugins/_announce's small JSON store ('air_control',
key 'words'), saved a few seconds after the last change. Saving is best
effort: a failure costs the memory of a few words, never an error.
"""
from __future__ import annotations

import re
import threading
import unicodedata

_EN = """
the be to of and a in that have I it for not on with he as you do at this but his by from
they we say her she or an will my one all would there their what so up out if about who get
which go me when make can like time no just him know take people into year your good some
could them see other than then now look only come its over think also back after use two how
our work first well way even new want because any these give day most us is was are been has
had were said did got made went came saw thought told took found gave knew felt left seemed
yes hello hi thanks thank please sorry okay ok sure great nice cool fine right wrong today
tomorrow tonight morning evening night week month weekend monday tuesday wednesday thursday
friday saturday sunday january february march april may june july august september october
november december love life world house home family friend friends child children man woman
men women boy girl baby mother father mom dad brother sister son daughter wife husband
name place thing things part case point group number fact hand eye eyes head face side
area water room money story book job business problem question night home school state
country city town street road car bus train plane office company team game music movie
film show party meeting call email message phone text chat video photo picture page site
website link file folder document report project plan idea time times minute minutes hour
hours second seconds moment while long little big small great old young high low large
early late last next same different important able best better sure free full real whole
open close start stop help need feel try leave call keep let begin seem turn show hear
play run move live believe hold bring happen write provide sit stand lose pay meet include
continue set learn change lead understand watch follow create speak read allow add spend
grow offer remember consider appear buy wait serve die send expect build stay fall cut
reach kill remain suggest raise pass sell require decide return explain hope develop carry
break receive agree support hit produce eat cover catch draw choose cause point listen
finish check fix test update save share post reply forward delete edit print order book
cook clean wash drive walk ride fly travel visit rest sleep wake dream smile laugh cry
here there where why how what when who whom whose which while until since though although
before after during under above below between among through across along around behind
beyond near far inside outside without within against toward towards upon onto off down
very really quite rather pretty too enough almost already always never often sometimes
usually ever still yet again soon later once twice maybe perhaps probably actually
especially exactly finally simply certainly clearly quickly slowly easily together alone
else instead anyway however therefore otherwise besides meanwhile indeed
something anything nothing everything someone anyone everyone nobody somebody anybody
everybody somewhere anywhere everywhere nowhere each every both either neither few many
much more less least several such own another enough half
one two three four five six seven eight nine ten eleven twelve twenty thirty hundred
thousand million first second third fourth fifth last
i'm you're we're they're it's that's there's what's let's don't doesn't didn't can't
couldn't won't wouldn't shouldn't isn't aren't wasn't weren't haven't hasn't hadn't i'll
you'll we'll i've you've we've i'd you'd
am being having doing going getting making taking coming looking thinking working using
trying asking telling saying seeing giving finding feeling leaving calling playing
running reading writing talking waiting watching sending sharing checking
food lunch dinner breakfast coffee tea beer wine pizza bread cake fruit apple chicken
health doctor hospital medicine body heart mind brain blood pain
weather rain snow sun sunny cold hot warm cool wind cloudy
color red blue green yellow black white brown orange pink purple gray
power energy light dark fire air earth space sky sea ocean river lake mountain tree
paper pen table chair door window wall floor bed kitchen bathroom garden
computer laptop screen keyboard mouse internet online offline app apps software code
data system network server password account login user users settings button
email emails address number numbers list lists note notes event events calendar reminder
price cost costs pay paid cheap expensive sale shop store market bank card cash
question questions answer answers reason reasons result results example examples
information news issue issues detail details problem problems solution solutions
kind sort type way ways step steps level levels rate rates line lines word words
history future past present today's minute social public private personal political
national international local general special simple easy hard difficult possible
available ready happy sad tired busy late early clear true false fast slow quick strong
weak beautiful pretty ugly short tall long wide deep heavy light cheap safe dangerous
interesting boring funny serious strange normal common popular famous favorite
amazing awesome perfect terrible horrible wonderful excellent fantastic brilliant
congratulations birthday holiday holidays vacation trip journey flight hotel ticket
tickets airport station
welcome goodbye bye cheers regards best wishes kind dear hey
just only also even still too either
mr mrs ms dr
because since unless whether
mean means meant hear heard ask asked tell told let lets put sent seen known shown
given taken written spoken chosen broken forgotten gotten done gone
attention client customer customers service services product products value values
market management development experience education research science technology
government policy law rights war peace art culture language english french spanish
german italian chinese japanese
student students teacher class lesson course exam homework university college
deadline schedule tomorrow's yesterday yesterday's agenda minutes summary review
draft final version feedback approval approved confirm confirmed cancel cancelled
available unavailable office remote meeting meetings conference presentation slides
""".split()

_FR = """
le la les de des du un une et à en est que qui dans pour pas sur au avec il elle je tu
nous vous ils elles on ce cette ces son sa ses mon ma mes ton ta tes notre nos votre vos
leur leurs se ne plus par mais ou donc or ni car comme tout tous toute toutes très bien
aussi être avoir faire dire aller voir savoir pouvoir vouloir venir devoir prendre
trouver donner parler mettre passer croire tenir porter rester partir penser regarder
attendre chercher entendre comprendre connaître sortir arriver aimer appeler demander
répondre écrire lire vivre suivre commencer finir ouvrir fermer envoyer recevoir payer
acheter manger boire dormir travailler jouer marcher courir oublier rappeler essayer
suis es sommes êtes sont étais était étions étiez étaient serai sera serons seront
ai as avons avez ont avais avait avions aviez avaient aurai aura aurons auront
fais fait faisons faites font vais va allons allez vont peux peut pouvons pouvez peuvent
veux veut voulons voulez veulent dois doit devons devez doivent sais sait savons savez
savent viens vient venons venez viennent prends prend prenons prenez prennent
bonjour bonsoir salut merci beaucoup oui non peut-être d'accord désolé pardon
s'il te plaît vous plaît bienvenue bravo félicitations
aujourd'hui demain hier maintenant toujours jamais souvent parfois encore déjà bientôt
tard tôt ici là où quand comment pourquoi combien quoi quel quelle quels quelles
avant après pendant depuis jusqu'à entre sous sans chez vers contre selon
moi toi lui eux soi même autre autres chaque quelque quelques plusieurs certains aucun
rien personne quelqu'un quelque chose chose choses
homme femme enfant enfants ami amie amis famille père mère frère sœur fils fille mari
monde vie jour jours nuit matin soir semaine mois année an ans heure heures minute
minutes temps moment fois
maison chambre cuisine bureau école ville pays rue route voiture train avion gare
travail argent problème question réponse idée raison exemple partie place point
nom numéro adresse message messages courriel mail téléphone photo vidéo lien fichier
dossier document page site réunion rendez-vous projet équipe client clients
lundi mardi mercredi jeudi vendredi samedi dimanche janvier février mars avril mai
juin juillet août septembre octobre novembre décembre
bon bonne bons bonnes mauvais grand grande petit petite nouveau nouvelle vieux vieille
beau belle jeune premier première dernier dernière long longue haut bas vrai faux
facile difficile possible important importante prêt prête content contente heureux
fatigué occupé libre seul seule meilleur pire même différent simple
un deux trois quatre cinq six sept huit neuf dix vingt cent mille
c'est n'est j'ai j'avais je suis qu'il qu'elle qu'on d'un d'une l'on jusqu'
eau café thé pain vin repas déjeuner dîner petit-déjeuner
chaud froid beau temps pluie soleil neige
santé médecin hôpital
merci beaucoup bonne journée bonne soirée bisous cordialement à bientôt
envoyé reçu fini commencé terminé prévu possible
vraiment seulement surtout ensemble ailleurs partout presque assez trop peu
alors ensuite enfin puis cependant pourtant quand même donc ainsi
voilà voici tiens bah ben bof
""".split()


def _dedupe(words: list) -> list:
    seen, out = set(), []
    for w in words:
        k = w.lower()
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


COMMON = {"en": _dedupe(_EN), "fr": _dedupe(_FR)}

STORE = "air_control"
SAVE_DELAY = 3.0
MAX_LEARNED = 3000
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", re.UNICODE)


def fold(text: str) -> str:
    """Lower case without accents: 'Été' -> 'ete'."""
    t = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c)).replace("’", "'")


def lang_for(layout: str = "qwerty") -> str:
    """'fr' for AZERTY or a French-first assistant, else 'en'."""
    if str(layout or "").lower() == "azerty":
        return "fr"
    try:
        from memory.config_manager import get_active_languages
        langs = get_active_languages()
        if langs and str(langs[0]).split("-")[0] in COMMON:
            return str(langs[0]).split("-")[0]
    except Exception:
        pass
    return "en"


def _match_case(word: str, prefix: str) -> str:
    if len(prefix) > 1 and prefix.isupper():
        return word.upper()
    if prefix[:1].isupper():
        return word[:1].upper() + word[1:]
    return word


class WordPredictor:
    """suggest(prefix) -> up to n words; learn(text) remembers what the user types."""

    def __init__(self, lang: str = "en", store: bool = True):
        self.lang = lang if lang in COMMON else "en"
        self.store = bool(store)
        base = COMMON[self.lang]
        n = max(1, len(base))
        # Rank score in (0, 1]: the most frequent word scores 1.
        self._base = {w: 1.0 - i / n for i, w in enumerate(base)}
        self._folded = [(fold(w), w) for w in base]
        self.learned: dict = {}
        self._loaded = not self.store
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    # ── persistence (best effort) ────────────────────────────────────────────
    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from plugins import _announce
            data = _announce.load(STORE, {}) or {}
            words = data.get("words") if isinstance(data, dict) else None
            if isinstance(words, dict):
                for w, c in words.items():
                    try:
                        self.learned[str(w).lower()] = max(0, int(c))
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass

    def _schedule_save(self) -> None:
        if not self.store:
            return
        try:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(SAVE_DELAY, self.flush)
            self._timer.daemon = True
            self._timer.start()
        except Exception:
            pass

    def flush(self) -> None:
        if not self.store:
            return
        try:
            from plugins import _announce
            with self._lock:
                words = dict(self.learned)
            data = _announce.load(STORE, {}) or {}
            if not isinstance(data, dict):
                data = {}
            data["words"] = words
            _announce.save(STORE, data)
        except Exception:
            pass

    # ── the two calls ────────────────────────────────────────────────────────
    def learn(self, text: str) -> None:
        try:
            self._load()
            found = [w.lower() for w in _WORD_RE.findall(str(text or "")) if len(w) >= 2]
            if not found:
                return
            with self._lock:
                for w in found:
                    self.learned[w] = self.learned.get(w, 0) + 1
                if len(self.learned) > MAX_LEARNED:
                    keep = sorted(self.learned.items(), key=lambda kv: -kv[1])[:MAX_LEARNED]
                    self.learned = dict(keep)
            self._schedule_save()
        except Exception:
            pass

    def suggest(self, prefix: str, n: int = 3) -> list:
        try:
            self._load()
            p = str(prefix or "")
            fp = fold(p)
            if not fp:
                return []
            scores: dict = {}
            for f, w in self._folded:
                if f.startswith(fp) and f != fp:
                    scores[w] = self._base.get(w, 0.0)
            with self._lock:
                learned = dict(self.learned)
            for w, c in learned.items():
                f = fold(w)
                if f.startswith(fp) and f != fp:
                    scores[w] = scores.get(w, self._base.get(w, 0.0)) + 2.0 * c
            ranked = sorted(scores.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
            return [_match_case(w, p) for w, _ in ranked[:max(0, int(n))]]
        except Exception:
            return []
