Main commands.
1. Take a snapshot: python3 prom_snapshot.py snapshot .
2. List all snapshots: python3 prom_snapshot.py list .
3. Compare two snapshots: python3 prom_snapshot.py compare *snapshot path* *snapshot path* -v . 
4. Compare latest with another one: python3 prom_snapshot.py compare latest *snapshot path* -v .
5. Output as JSON: python3 prom_snapshot.py snapshot --json .
6. Compare 2 snapshots in JSON: python3 prom_snapshot.py compare file1.yaml file2.yaml --json .
7. Help : python3 prom_snapshot.py --help, python3 prom_snapshot.py snapshot --help.
