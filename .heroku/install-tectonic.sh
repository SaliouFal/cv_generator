set -e
echo "→ Installing Tectonic LaTeX compiler…"
curl -L https://github.com/tectonic-typesetting/tectonic/releases/latest/download/tectonic-x86_64-unknown-linux-musl.tar.gz \
  | tar xz -C /tmp
install -Dm755 /tmp/tectonic*/tectonic $HOME/bin/tectonic
echo "✓ Tectonic installed in \$HOME/bin"
