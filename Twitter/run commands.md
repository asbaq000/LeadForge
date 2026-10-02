To include all already seen ones as well
python -m x_leads --hours 48 -f csv -o leads.csv --include-seen

python -m x_leads --hours 48 -f csv -o leads.csv 

Country filter. It reports by default and drops nothing - run it once and read
the "location:" line on stderr to see how many authors it can actually place,
then switch to drop.
python -m x_leads --hours 48 -f csv -o leads.csv --country-filter drop

Also cut Egypt and Nepal
python -m x_leads --hours 48 -f csv -o leads.csv --country-filter drop --exclude-country egypt,nepal

Check what the country filter removed, and why
python -m x_leads --hours 48 --country-filter drop --include-rejected