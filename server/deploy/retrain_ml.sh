#!/usr/bin/env bash
# Ежемесячное переобучение ML-модели агента (запускает таймер leoagent-retrain).
# Обновляет историю и сигналы, обучает модель на всей истории и ставит её
# агенту, только если точность вне выборки за последний год не ниже порога.
# Нужна исследовательская среда /root/research/.venv-ml (torch не нужен:
# хватает pandas, pandas-ta, scikit-learn, lightgbm).

set -euo pipefail

AGENT=/opt/leoagent
RESEARCH=/root/research
MODELS=$AGENT/data/models
MIN_AUC=0.51

if [ ! -x "$RESEARCH/.venv-ml/bin/python" ]; then
  echo "Нет $RESEARCH/.venv-ml — переобучение пропущено" >&2
  exit 1
fi

# Код — ровно тот же, что у агента: признаки при обучении и в торговле совпадают
mkdir -p "$RESEARCH"
rsync -a --delete "$AGENT/app/" "$RESEARCH/app/"
rsync -a --delete "$AGENT/research/" "$RESEARCH/research/"
cp "$AGENT/backtest.py" "$RESEARCH/backtest.py"

cd "$RESEARCH"
export PYTHONIOENCODING=utf-8 OMP_NUM_THREADS=3
PY="nice -n 15 $RESEARCH/.venv-ml/bin/python"
$PY backtest.py --strategy agent_h1 --refresh --prepare-only --workers 3
$PY research/train_ml.py --out "$RESEARCH/data/models_new"

AUC=$(python3 -c "import json; print(json.load(open('$RESEARCH/data/models_new/ml_h6.json'))['auc_oof_last_year'])")
if python3 -c "import sys; sys.exit(0 if $AUC >= $MIN_AUC else 1)"; then
  mkdir -p "$MODELS"
  for f in "$MODELS"/ml_h6.json "$MODELS"/ml_h6.txt; do
    [ -f "$f" ] && cp "$f" "$f.prev"
  done
  cp "$RESEARCH"/data/models_new/ml_h6.* "$MODELS"/
  chown -R leoagent:leoagent "$MODELS"
  systemctl restart leoagent
  echo "Новая модель установлена: AUC вне выборки за год $AUC"
else
  echo "Модель не установлена: AUC за год $AUC ниже $MIN_AUC, работает прежняя" >&2
fi
