"""Countries and their major cities, for the pickers in the UI.

The country list is complete, because you should be able to prospect anywhere.
The city lists are not: they are a starting shortlist for the markets people
actually work, and the city box stays free-text so anywhere else can simply be
typed in. Whatever is chosen goes to the geocoder as "City, Country", which is
the form OpenStreetMap resolves most reliably.
"""

from __future__ import annotations

_COUNTRIES = """
AF|Afghanistan
AL|Albania
DZ|Algeria
AD|Andorra
AO|Angola
AG|Antigua and Barbuda
AR|Argentina
AM|Armenia
AU|Australia
AT|Austria
AZ|Azerbaijan
BS|Bahamas
BH|Bahrain
BD|Bangladesh
BB|Barbados
BY|Belarus
BE|Belgium
BZ|Belize
BJ|Benin
BT|Bhutan
BO|Bolivia
BA|Bosnia and Herzegovina
BW|Botswana
BR|Brazil
BN|Brunei
BG|Bulgaria
BF|Burkina Faso
BI|Burundi
KH|Cambodia
CM|Cameroon
CA|Canada
CV|Cape Verde
CF|Central African Republic
TD|Chad
CL|Chile
CN|China
CO|Colombia
KM|Comoros
CG|Congo
CD|Congo (DRC)
CR|Costa Rica
HR|Croatia
CU|Cuba
CY|Cyprus
CZ|Czechia
DK|Denmark
DJ|Djibouti
DM|Dominica
DO|Dominican Republic
EC|Ecuador
EG|Egypt
SV|El Salvador
GQ|Equatorial Guinea
ER|Eritrea
EE|Estonia
SZ|Eswatini
ET|Ethiopia
FJ|Fiji
FI|Finland
FR|France
GA|Gabon
GM|Gambia
GE|Georgia
DE|Germany
GH|Ghana
GR|Greece
GD|Grenada
GT|Guatemala
GN|Guinea
GW|Guinea-Bissau
GY|Guyana
HT|Haiti
HN|Honduras
HK|Hong Kong
HU|Hungary
IS|Iceland
IN|India
ID|Indonesia
IR|Iran
IQ|Iraq
IE|Ireland
IL|Israel
IT|Italy
CI|Ivory Coast
JM|Jamaica
JP|Japan
JO|Jordan
KZ|Kazakhstan
KE|Kenya
KI|Kiribati
KW|Kuwait
KG|Kyrgyzstan
LA|Laos
LV|Latvia
LB|Lebanon
LS|Lesotho
LR|Liberia
LY|Libya
LI|Liechtenstein
LT|Lithuania
LU|Luxembourg
MO|Macau
MG|Madagascar
MW|Malawi
MY|Malaysia
MV|Maldives
ML|Mali
MT|Malta
MH|Marshall Islands
MR|Mauritania
MU|Mauritius
MX|Mexico
FM|Micronesia
MD|Moldova
MC|Monaco
MN|Mongolia
ME|Montenegro
MA|Morocco
MZ|Mozambique
MM|Myanmar
NA|Namibia
NR|Nauru
NP|Nepal
NL|Netherlands
NZ|New Zealand
NI|Nicaragua
NE|Niger
NG|Nigeria
KP|North Korea
MK|North Macedonia
NO|Norway
OM|Oman
PK|Pakistan
PW|Palau
PS|Palestine
PA|Panama
PG|Papua New Guinea
PY|Paraguay
PE|Peru
PH|Philippines
PL|Poland
PT|Portugal
PR|Puerto Rico
QA|Qatar
RO|Romania
RU|Russia
RW|Rwanda
KN|Saint Kitts and Nevis
LC|Saint Lucia
VC|Saint Vincent and the Grenadines
WS|Samoa
SM|San Marino
ST|Sao Tome and Principe
SA|Saudi Arabia
SN|Senegal
RS|Serbia
SC|Seychelles
SL|Sierra Leone
SG|Singapore
SK|Slovakia
SI|Slovenia
SB|Solomon Islands
SO|Somalia
ZA|South Africa
KR|South Korea
SS|South Sudan
ES|Spain
LK|Sri Lanka
SD|Sudan
SR|Suriname
SE|Sweden
CH|Switzerland
SY|Syria
TW|Taiwan
TJ|Tajikistan
TZ|Tanzania
TH|Thailand
TL|Timor-Leste
TG|Togo
TO|Tonga
TT|Trinidad and Tobago
TN|Tunisia
TR|Turkey
TM|Turkmenistan
TV|Tuvalu
UG|Uganda
UA|Ukraine
AE|United Arab Emirates
GB|United Kingdom
US|United States
UY|Uruguay
UZ|Uzbekistan
VU|Vanuatu
VA|Vatican City
VE|Venezuela
VN|Vietnam
YE|Yemen
ZM|Zambia
ZW|Zimbabwe
"""

# Major cities per country. Ordered roughly by how much business there is to
# find, not by population, since that is what a prospecting list wants.
_CITIES = {
    "PK": "Karachi, Lahore, Islamabad, Rawalpindi, Faisalabad, Multan, Peshawar, "
          "Quetta, Sialkot, Gujranwala, Hyderabad, Bahawalpur, Sargodha, Abbottabad",
    "US": "New York, Los Angeles, Chicago, Houston, Phoenix, Philadelphia, "
          "San Antonio, San Diego, Dallas, Austin, Jacksonville, San Jose, "
          "Columbus, Charlotte, Indianapolis, Seattle, Denver, Boston, Nashville, "
          "Atlanta, Miami, Las Vegas, Portland, Orlando, Tampa",
    "GB": "London, Birmingham, Manchester, Leeds, Glasgow, Liverpool, Bristol, "
          "Sheffield, Edinburgh, Cardiff, Leicester, Nottingham, Newcastle, "
          "Belfast, Southampton, Brighton, Coventry, Reading",
    "CA": "Toronto, Montreal, Vancouver, Calgary, Edmonton, Ottawa, Winnipeg, "
          "Quebec City, Hamilton, Mississauga, Brampton, Surrey, Halifax, London",
    "AU": "Sydney, Melbourne, Brisbane, Perth, Adelaide, Gold Coast, Canberra, "
          "Newcastle, Wollongong, Geelong, Hobart, Townsville, Cairns, Darwin",
    "AE": "Dubai, Abu Dhabi, Sharjah, Ajman, Al Ain, Ras Al Khaimah, Fujairah, "
          "Umm Al Quwain",
    "SA": "Riyadh, Jeddah, Mecca, Medina, Dammam, Khobar, Taif, Tabuk, Abha, "
          "Buraidah, Jubail, Khamis Mushait",
    "IN": "Mumbai, Delhi, Bengaluru, Hyderabad, Chennai, Kolkata, Pune, "
          "Ahmedabad, Jaipur, Surat, Lucknow, Chandigarh, Indore, Kochi, Noida, "
          "Gurugram",
    "IE": "Dublin, Cork, Limerick, Galway, Waterford, Drogheda, Dundalk, Swords",
    "NZ": "Auckland, Wellington, Christchurch, Hamilton, Tauranga, Dunedin, "
          "Palmerston North, Napier, Nelson, Queenstown",
    "ZA": "Johannesburg, Cape Town, Durban, Pretoria, Port Elizabeth, "
          "Bloemfontein, East London, Polokwane, Nelspruit",
    "DE": "Berlin, Hamburg, Munich, Cologne, Frankfurt, Stuttgart, Dusseldorf, "
          "Dortmund, Essen, Leipzig, Bremen, Dresden, Hanover, Nuremberg",
    "FR": "Paris, Marseille, Lyon, Toulouse, Nice, Nantes, Montpellier, "
          "Strasbourg, Bordeaux, Lille, Rennes, Toulon",
    "ES": "Madrid, Barcelona, Valencia, Seville, Zaragoza, Malaga, Murcia, "
          "Palma, Bilbao, Alicante, Cordoba, Granada, Marbella",
    "IT": "Rome, Milan, Naples, Turin, Palermo, Genoa, Bologna, Florence, "
          "Bari, Catania, Venice, Verona",
    "NL": "Amsterdam, Rotterdam, The Hague, Utrecht, Eindhoven, Tilburg, "
          "Groningen, Almere, Breda, Nijmegen",
    "BE": "Brussels, Antwerp, Ghent, Charleroi, Liege, Bruges, Namur, Leuven",
    "PT": "Lisbon, Porto, Braga, Coimbra, Funchal, Faro, Setubal, Aveiro",
    "CH": "Zurich, Geneva, Basel, Bern, Lausanne, Lucerne, St. Gallen, Lugano",
    "AT": "Vienna, Graz, Linz, Salzburg, Innsbruck, Klagenfurt, Villach",
    "SE": "Stockholm, Gothenburg, Malmo, Uppsala, Vasteras, Orebro, Linkoping",
    "NO": "Oslo, Bergen, Trondheim, Stavanger, Drammen, Kristiansand, Tromso",
    "DK": "Copenhagen, Aarhus, Odense, Aalborg, Esbjerg, Randers, Kolding",
    "FI": "Helsinki, Espoo, Tampere, Vantaa, Oulu, Turku, Jyvaskyla, Lahti",
    "PL": "Warsaw, Krakow, Lodz, Wroclaw, Poznan, Gdansk, Szczecin, Katowice, "
          "Lublin, Bydgoszcz",
    "TR": "Istanbul, Ankara, Izmir, Bursa, Antalya, Adana, Konya, Gaziantep, "
          "Mersin, Kayseri",
    "EG": "Cairo, Alexandria, Giza, Shubra El Kheima, Port Said, Suez, Luxor, "
          "Mansoura, Tanta, Aswan",
    "NG": "Lagos, Abuja, Kano, Ibadan, Port Harcourt, Benin City, Kaduna, "
          "Enugu, Onitsha, Abeokuta",
    "KE": "Nairobi, Mombasa, Kisumu, Nakuru, Eldoret, Thika, Malindi, Kitale",
    "MA": "Casablanca, Rabat, Marrakesh, Fez, Tangier, Agadir, Meknes, Oujda",
    "QA": "Doha, Al Rayyan, Al Wakrah, Al Khor, Umm Salal, Lusail",
    "KW": "Kuwait City, Hawalli, Salmiya, Farwaniya, Jahra, Ahmadi, Fahaheel",
    "BH": "Manama, Riffa, Muharraq, Hamad Town, Isa Town, Sitra",
    "OM": "Muscat, Salalah, Sohar, Nizwa, Sur, Ibri, Barka",
    "JO": "Amman, Zarqa, Irbid, Aqaba, Russeifa, Madaba",
    "MY": "Kuala Lumpur, George Town, Johor Bahru, Ipoh, Shah Alam, Petaling Jaya, "
          "Kuching, Kota Kinabalu, Malacca City",
    "SG": "Singapore",
    "ID": "Jakarta, Surabaya, Bandung, Medan, Semarang, Palembang, Makassar, "
          "Denpasar, Yogyakarta, Batam",
    "PH": "Manila, Quezon City, Davao City, Cebu City, Makati, Taguig, Pasig, "
          "Caloocan, Bacolod, Iloilo City",
    "TH": "Bangkok, Chiang Mai, Pattaya, Phuket, Nonthaburi, Hat Yai, Khon Kaen",
    "VN": "Ho Chi Minh City, Hanoi, Da Nang, Hai Phong, Can Tho, Nha Trang, Hue",
    "BD": "Dhaka, Chittagong, Khulna, Rajshahi, Sylhet, Barisal, Rangpur, Comilla",
    "LK": "Colombo, Kandy, Galle, Jaffna, Negombo, Dehiwala, Kurunegala",
    "NP": "Kathmandu, Pokhara, Lalitpur, Biratnagar, Bharatpur, Birgunj",
    "JP": "Tokyo, Osaka, Yokohama, Nagoya, Sapporo, Fukuoka, Kobe, Kyoto, "
          "Hiroshima, Sendai",
    "KR": "Seoul, Busan, Incheon, Daegu, Daejeon, Gwangju, Suwon, Ulsan",
    "CN": "Shanghai, Beijing, Shenzhen, Guangzhou, Chengdu, Hangzhou, Wuhan, "
          "Xi'an, Nanjing, Qingdao",
    "HK": "Hong Kong, Kowloon, Tsuen Wan, Sha Tin, Tuen Mun",
    "BR": "Sao Paulo, Rio de Janeiro, Brasilia, Salvador, Fortaleza, Belo Horizonte, "
          "Curitiba, Recife, Porto Alegre, Manaus",
    "MX": "Mexico City, Guadalajara, Monterrey, Puebla, Tijuana, Cancun, Merida, "
          "Queretaro, Leon, Toluca",
    "AR": "Buenos Aires, Cordoba, Rosario, Mendoza, La Plata, Mar del Plata, Salta",
    "CL": "Santiago, Valparaiso, Concepcion, La Serena, Antofagasta, Temuco",
    "CO": "Bogota, Medellin, Cali, Barranquilla, Cartagena, Bucaramanga, Pereira",
    "PE": "Lima, Arequipa, Trujillo, Chiclayo, Piura, Cusco, Iquitos",
    "GR": "Athens, Thessaloniki, Patras, Heraklion, Larissa, Volos, Rhodes",
    "RO": "Bucharest, Cluj-Napoca, Timisoara, Iasi, Constanta, Craiova, Brasov",
    "CZ": "Prague, Brno, Ostrava, Plzen, Liberec, Olomouc, Ceske Budejovice",
    "HU": "Budapest, Debrecen, Szeged, Miskolc, Pecs, Gyor, Nyiregyhaza",
    "IL": "Tel Aviv, Jerusalem, Haifa, Rishon LeZion, Petah Tikva, Netanya, Eilat",
    "RU": "Moscow, Saint Petersburg, Novosibirsk, Yekaterinburg, Kazan, "
          "Nizhny Novgorod, Samara, Rostov-on-Don",
    "UA": "Kyiv, Kharkiv, Odesa, Dnipro, Lviv, Zaporizhzhia, Vinnytsia",
    "GH": "Accra, Kumasi, Tamale, Takoradi, Cape Coast, Tema",
    "TZ": "Dar es Salaam, Dodoma, Mwanza, Arusha, Zanzibar City, Mbeya",
    "UG": "Kampala, Gulu, Mbarara, Jinja, Entebbe, Mbale",
}


def countries() -> list[dict]:
    """Every country, each with whatever shortlist of cities we have."""
    out = []
    for line in _COUNTRIES.strip().splitlines():
        code, _, name = line.partition("|")
        cities = [c.strip() for c in _CITIES.get(code, "").split(",") if c.strip()]
        out.append({"code": code, "name": name, "cities": cities})
    return out


def cities_for(code: str) -> list[str]:
    return [c.strip() for c in _CITIES.get(code.upper(), "").split(",") if c.strip()]


def country_name(code: str) -> str:
    for line in _COUNTRIES.strip().splitlines():
        c, _, name = line.partition("|")
        if c == code.upper():
            return name
    return ""
