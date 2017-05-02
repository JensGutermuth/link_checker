# Check all links on your website

Recursively crawls your website looking for broken links.

## Usage

```
git clone https://github.com/Delphinator/link_checker.git
cd link_checker
virtualenv --python=/usr/bin/python3 venv
source venv/bin/activate
pip install -r requirements.txt
./link_checker.py <start_url>...
```

Alternatively use `link_checker.py` without arguments to check for dead links in the files in the current working directory.
