# Flare early-warning: model comparison

Task: at a flare's X-ray peak, will it drive a ≥10 pfu proton storm? Features: log_peak, log_fluence, log_rise_min, log_background, connected, has_location, abs_lat.
Train 2022-06-17 to 2024-07-01 (822 flares, 13 storms); test 2024-07-01 to 2026-09-25 (1082 flares, 11 storms). Hyperparameters and thresholds use training data only (grid search on out-of-fold average precision, half-year blocks of the training period).

| Model | Test ROC-AUC (95% CI) | Test PR-AUC | High-recall mode: storms caught / warnings | Balanced mode: storms / warnings | Top 3/month: storms caught | Warnings to catch all |
|---|---|---|---|---|---|---|
| Logistic regression (L2) | 0.987 (0.98–0.99) | 0.418 | 11/11 · 173 (6.5/mo) | 8/11 · 29 | 11/11 | 49 |
| Logistic regression (L1, sparse) | 0.985 (0.97–0.99) | 0.415 | 11/11 · 185 (6.9/mo) | 10/11 · 42 | 11/11 | 78 |
| Gradient boosting (histogram) | 0.979 (0.95–0.99) | 0.386 | 11/11 · 342 (12.8/mo) | 9/11 · 32 | 10/11 | 138 |
| Gaussian naive Bayes | 0.982 (0.97–0.99) | 0.370 | 11/11 · 175 (6.5/mo) | 6/11 · 17 | 10/11 | 98 |
| Current app model (numpy logistic) | 0.977 (0.96–0.99) | 0.329 | 11/11 · 230 (8.6/mo) | 7/11 · 30 | 10/11 | 84 |
| Random forest | 0.979 (0.96–0.99) | 0.316 | 11/11 · 380 (14.2/mo) | 9/11 · 42 | 10/11 | 109 |
| SVM (RBF kernel) | 0.977 (0.96–0.99) | 0.263 | 11/11 · 144 (5.4/mo) | 5/11 · 18 | 11/11 | 76 |
| Extra trees | 0.979 (0.97–0.99) | 0.232 | 11/11 · 423 (15.8/mo) | 9/11 · 39 | 11/11 | 71 |
| Neural network (MLP) | 0.971 (0.95–0.99) | 0.212 | 11/11 · 246 (9.2/mo) | 9/11 · 76 | 9/11 | 133 |
| Rule: flare peak brightness only | 0.933 (0.86–0.98) | 0.211 | 11/11 · 544 (20.3/mo) | 3/11 · 7 | 9/11 | 435 |
| k-nearest neighbors | 0.918 (0.80–0.98) | 0.202 | 11/11 · 1082 (40.4/mo) | 10/11 · 90 | 9/11 | 553 |

High-recall mode: threshold that caught ≥90% of training storms out-of-fold. Balanced mode: threshold maximizing the Heidke skill score out-of-fold. Top 3/month: the model's 80 highest-scoring test flares. PR-AUC matters most here because storms are ~1% of flares.
With 11 test storms, differences of one or two storms are within noise; see the ROC-AUC intervals.
