# Pi uses the native SDK extension

Do not run Pi through this portable driver or `pi -p`. Install the native
in-process `AgentSession` extension:

```bash
./install.sh --pi
```

Then run `/reload` and `/trio <goal>` in interactive Pi. See `SETUP-BY-PI.md`.
