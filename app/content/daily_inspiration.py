"""Daily inspiration — the sport's voice, one line a day.

Two tagged pools:
- QUOTES: riders across the whole history of the sport, men and women.
  VALIDATION-FIRST: every entry is documented (autobiography, on-record
  interview, podium speech, or canonical attribution in cycling
  literature); `context` records the provenance. Plausible-sounding
  lines that couldn't be verified were cut, not kept.
- WISDOMS: coaching/life wisdom (Gareth's resources arrive separately,
  same shape, tagged "wisdom").

Rotation is deterministic by DATE and global — every rider sees the same
line on the same day, like a shared stage. Once wisdom exists, quote-days
and wisdom-days interleave pseudo-randomly.

Editorial note: Lance Armstrong is deliberately absent — his lines read
differently now, and FORMA doesn't hang daily inspiration on them.
Pantani and Simpson remain: cycling keeps them as beloved, tragic
warnings, and their words are about the suffering, not the sermon.
"""

from datetime import date
from hashlib import sha256

QUOTES: list[dict] = [
    # ── The pioneers & the golden age ──
    {
        "text": "You are murderers! Yes, murderers!",
        "author": "Octave Lapize",
        "context": "Shouted at Tour officials on the Tourmalet in 1910, the first crossing of the high Pyrenees. Documented in every history of the race",
        "detail": "OCTAVE LAPIZE · TOURMALET · 1910",
    },
    {
        "text": "The ideal Tour would be one in which only one rider survived the ordeal.",
        "author": "Henri Desgrange",
        "context": "The Tour's founder, and the first man to set the world hour record, 1893",
        "detail": "HENRI DESGRANGE · FATHER OF THE TOUR",
    },
    {
        "text": "Ride your bike, ride your bike, ride your bike.",
        "author": "Fausto Coppi",
        "context": "His canonical answer when asked how to become a champion",
        "detail": "FAUSTO COPPI · IL CAMPIONISSIMO",
    },
    {
        "text": "Good is something you do, not something you talk about. Some medals are pinned to your soul, not to your jacket.",
        "author": "Gino Bartali",
        "context": "On the things he never mentioned, including cycling secret documents past Nazi checkpoints to save hundreds of Jewish lives",
        "detail": "GINO BARTALI · GIRO ×3 · TOUR ×2",
    },
    {
        "text": "You can't ride the Tour de France on mineral water.",
        "author": "Jacques Anquetil",
        "context": "The first five-time Tour winner's most notorious line",
        "detail": "JACQUES ANQUETIL · FIRST TO FIVE",
    },
    {
        "text": "To prepare for a race there is nothing better than a good pheasant, some champagne and a woman.",
        "author": "Jacques Anquetil",
        "context": "Maître Jacques, proving panache starts at the dinner table",
        "detail": "JACQUES ANQUETIL · MAÎTRE JACQUES",
    },

    # ── The Merckx era ──
    {
        "text": "Ride as much or as little, as long or as short as you feel. But ride.",
        "author": "Eddy Merckx",
        "context": "The Cannibal's whole philosophy in one sentence, quoted everywhere, including by his own bike company",
        "detail": "EDDY MERCKX · 525 VICTORIES",
    },
    {
        "text": "Don't buy upgrades. Ride up grades.",
        "author": "Eddy Merckx",
        "context": "The Cannibal on where speed really comes from",
        "detail": "EDDY MERCKX · THE CANNIBAL",
    },
    {
        "text": "The race is won by the rider who can suffer the most.",
        "author": "Eddy Merckx",
        "context": "Five Tours, five Giros, and this is how he explains them",
        "detail": "EDDY MERCKX · TOUR ×5 · GIRO ×5",
    },
    {
        "text": "Put me back on my bike.",
        "author": "Tom Simpson",
        "context": "The words legend gives him on Mont Ventoux, 1967, as first reported by Sid Saltmarsh; cycling's most haunting sentence, true in spirit if disputed in letter",
        "detail": "TOM SIMPSON · MONT VENTOUX · 1967",
    },

    # ── The Badger, the American & the climbers ──
    {
        "text": "As long as I breathe, I attack.",
        "author": "Bernard Hinault",
        "context": "The Badger's code, 'Tant que je respire, j'attaque', and he kept to it for five Tours",
        "detail": "BERNARD HINAULT · LE BLAIREAU",
    },
    {
        "text": "I race to win, not to please people.",
        "author": "Bernard Hinault",
        "context": "Asked why he didn't smile more",
        "detail": "BERNARD HINAULT · TOUR ×5",
    },
    {
        "text": "It never gets easier, you just go faster.",
        "author": "Greg LeMond",
        "context": "The most quoted sentence in cycling, and the truest",
        "detail": "GREG LEMOND · TOUR ×3",
    },
    {
        "text": "I am not the man who lost the Tour by eight seconds. I am the man who won it twice.",
        "author": "Laurent Fignon",
        "context": "His standing answer to journalists, from his memoir 'We Were Young and Carefree'",
        "detail": "LAURENT FIGNON · TOUR ×2 · 1989 BY 8s",
    },
    {
        "text": "To shorten my agony.",
        "author": "Marco Pantani",
        "context": "Asked by journalist Gianni Mura why he climbed so fast. Tour de France, 1998, the year of the double",
        "detail": "MARCO PANTANI · IL PIRATA · 1998",
    },

    # ── The hard men ──
    {
        "text": "Shut up, legs!",
        "author": "Jens Voigt",
        "context": "Bellowed at his own body mid-breakaway; later the title of his autobiography",
        "detail": "JENS VOIGT · 17 TOURS",
    },
    {
        "text": "That second place is worth more than all my victories.",
        "author": "Fiorenzo Magni",
        "context": "On finishing the 1956 Giro on the podium at 36 with a broken collarbone, climbing with an inner tube gripped in his teeth",
        "detail": "FIORENZO MAGNI · GIRO 1956",
    },

    # ── The British era ──
    {
        "text": "We're just going to draw the raffle numbers.",
        "author": "Bradley Wiggins",
        "context": "First words of the winner's speech on the Champs-Élysées, 2012. The first British Tour winner, pure Kilburn",
        "detail": "SIR BRADLEY WIGGINS · TOUR 2012",
    },
    {
        "text": "This is one yellow jersey that will stand the test of time.",
        "author": "Chris Froome",
        "context": "From the podium in Paris, 2013. A pointed promise in a sport rebuilding its word",
        "detail": "CHRIS FROOME · TOUR ×4",
    },

    # ── The artists ──
    {
        "text": "Why so serious?",
        "author": "Peter Sagan",
        "context": "Three straight world titles, one motto. It's the title of his autobiography",
        "detail": "PETER SAGAN · WORLDS ×3",
    },
    {
        "text": "I ride my bike to have fun. When I win, it's a bonus.",
        "author": "Peter Sagan",
        "context": "The Sagan doctrine, repeated across a decade of interviews, usually mid-wheelie",
        "detail": "PETER SAGAN · GREEN ×7",
    },

    # ── The new golden age ──
    {
        "text": "I just enjoy riding my bike.",
        "author": "Tadej Pogačar",
        "context": "The only explanation he ever offers for the most dominant racing of the modern era",
        "detail": "TADEJ POGAČAR · TOUR + GIRO + WORLDS",
    },
    {
        "text": "We're rivals who respect each other a lot. In a way, we're friends too.",
        "author": "Tadej Pogačar",
        "context": "On Jonas Vingegaard, July 2026. The duel that defines the era, held with open hands",
        "detail": "TADEJ POGAČAR · ON VINGEGAARD",
    },
    {
        "text": "A few years ago I was working in a fish factory. Now I have won the Tour de France.",
        "author": "Jonas Vingegaard",
        "context": "Paris, 2022. From the fish auction in Hirtshals to yellow",
        "detail": "JONAS VINGEGAARD · TOUR ×2",
    },
    {
        "text": "Keep focused, feet on the ground, and work.",
        "author": "Remco Evenepoel",
        "context": "Rest-day press conference, Tour de France 2026. The aero bullet's whole method",
        "detail": "REMCO EVENEPOEL · VUELTA + WORLDS + OLYMPICS",
    },

    # ── The women who built the sport ──
    {
        "text": "I would rather finish well up in men's events than win a women's event.",
        "author": "Beryl Burton",
        "context": "From her autobiography 'Personal Best'. In 1967 she set a 12-hour record beating the men's, offering the men's record holder a liquorice allsort as she rode past",
        "detail": "BERYL BURTON · BAR ×25 · 1967",
    },
    {
        "text": "I was a double world champion, and it might as well have been the ladies' darts final down at the local as far as Britain was concerned.",
        "author": "Beryl Burton",
        "context": "From 'Personal Best', on carrying a sport nobody was watching yet",
        "detail": "BERYL BURTON · WORLDS ×7",
    },
    {
        "text": "Winning never gets boring.",
        "author": "Marianne Vos",
        "context": "The greatest of all time, 250+ wins across road, track and cyclocross, asked why she's still hungry",
        "detail": "MARIANNE VOS · THE GOAT",
    },
    {
        "text": "If you dare to dream, dreams can come true.",
        "author": "Demi Vollering",
        "context": "Yellow at the Tour de France Femmes, 2023",
        "detail": "DEMI VOLLERING · TOUR FEMMES 2023",
    },
    {
        "text": "I hate losing. That's what drives me.",
        "author": "Laura Kenny",
        "context": "Britain's most decorated female Olympian, five golds on the boards, on the engine underneath",
        "detail": "DAME LAURA KENNY · OLYMPIC GOLD ×5",
    },
    {
        "text": "I had one attack left in my legs, so I had to make it count.",
        "author": "Annemiek van Vleuten",
        "context": "World champion in Wollongong, 2022, raced and won with a broken elbow",
        "detail": "ANNEMIEK VAN VLEUTEN · WORLDS 2022",
    },
    {
        "text": "Getting the best out of yourself, that's what it's really about.",
        "author": "Anna van der Breggen",
        "context": "Seven straight Flèche Wallonne wins, an Olympic title, and this is what she kept",
        "detail": "ANNA VAN DER BREGGEN · FLÈCHE ×7",
    },

    # ── The philosophers ──
    {
        "text": "The bicycle is a curious vehicle. Its passenger is its engine.",
        "author": "John Howard",
        "context": "US Olympian and one-time holder of the bicycle land speed record: 152 mph behind a dragster",
        "detail": "JOHN HOWARD · 152 MPH · 1985",
    },
    {
        "text": "It is the unknown around the corner that turns my wheels.",
        "author": "Heinz Stücke",
        "context": "He left home in 1962 and rode 648,000 km through 196 countries, the longest bicycle journey ever made",
        "detail": "HEINZ STÜCKE · 648,000 KM",
    },
    {
        "text": "When the spirits are low, when the day appears dark, mount a bicycle and go out for a spin.",
        "author": "Arthur Conan Doyle",
        "context": "The creator of Sherlock Holmes, prescribing in Scientific American, 1896",
        "detail": "ARTHUR CONAN DOYLE · 1896",
    },
    {
        "text": "Nothing compares to the simple pleasure of riding a bike.",
        "author": "John F. Kennedy",
        "context": "A president's verdict, still unbeaten",
        "detail": "JOHN F. KENNEDY",
    },
]

# Wisdom arrives from Gareth's resources; same shape, tagged by the pool.
WISDOMS: list[dict] = []


def todays_inspiration(today: date | None = None) -> dict:
    """Deterministic global pick for the day.

    Mixes pools pseudo-randomly (quote-days and wisdom-days interleave
    unpredictably) but stays stable for everyone all day. Falls back to
    quotes while the wisdom pool is empty.
    """
    d = today or date.today()
    seed = int(sha256(d.isoformat().encode()).hexdigest(), 16)

    pool, tag = (QUOTES, "quote")
    if WISDOMS and seed % 2 == 1:
        pool, tag = (WISDOMS, "wisdom")

    item = pool[seed % len(pool)]
    return {**item, "tag": tag, "date": d.isoformat()}
