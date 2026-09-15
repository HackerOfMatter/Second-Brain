# 📘 Corporate Finance Formula + Excel Function Reference (Student Edition)
> Quick-reference pairing core finance formulas with practical Excel syntax.  
> Tags: #finance #excel #formulas #quickref  
[[Formula Finder Finance]]

---

## 🕓 Chapter 4 – Time Value of Money
| Concept / Formula                    | Excel Function(s)           | Example Syntax                                 | What it Means                       |
| ------------------------------------ | --------------------------- | ---------------------------------------------- | ----------------------------------- |
| **Future Value** FV = PV(1+r)^t      | `FV`                        | `=FV(r, t, 0, -PV)` → `=FV(0.08, 5, 0, -1000)` | Value t periods from now at rate r  |
| **Present Value** PV = FV/(1+r)^t    | `PV`                        | `=PV(r, t, 0, -FV)`                            | Value today of future amount        |
| **Perpetuity** PV = C/r              | —                           | `=C/r` → `=150/0.06`                           | Level cash flow forever             |
| **Growing Perpetuity** PV = C₁/(r–g) | —                           | `=C1/(r-g)`                                    | Cash flow grows forever (r>g)       |
| **Annuity PV** = C[1-(1+r)^-t]/r     | `PV`, `PMT`, `NPER`, `RATE` | `=PV(r, t, -C, 0)`                             | Level payment for t periods         |
| **EAR = (1+APR/m)^m - 1**            | `EFFECT`, `NOMINAL`         | `=EFFECT(APR, m)`                              | Convert APR ↔ effective annual rate |

---

## 💰 Chapter 5 – Discounted Cash Flow Valuation
| Concept / Formula | Excel Function(s) | Example Syntax | What it Means |
|--------------------|------------------|----------------|---------------|
| **NPV = Σ CFₜ/(1+r)ᵗ - C₀** | `NPV`, `XNPV` | `=NPV(r, CF1:CFn)+C0`  `=XNPV(r, flows, dates)` | Present value of cash flows |
| **IRR: NPV = 0** | `IRR`, `XIRR` | `=IRR(range)`  `=XIRR(flows, dates)` | Discount rate that makes NPV = 0 |
| **Payback Period** | — | cumulative sum until ≥ 0 | Time to recover initial outlay |

---

## 🧾 Chapter 6 – Interest Rates & Bond Valuation
| Concept / Formula | Excel Function(s) | Example Syntax | What it Means |
|--------------------|------------------|----------------|---------------|
| **Bond Price P = Σ C/(1+r)ᵗ + F/(1+r)ᵀ** | `PRICE`, `YIELD` | `=PRICE(settle, mat, coupon, yld, 100, 2, 0)` | PV of coupons + face value |
| **Current Yield = Coupon/Price** | — | `=Coupon/Price` | Annual coupon ÷ market price |
| **YTM** | `YIELD`, `RATE` | `=RATE(n, coupon, -price, face)` | Return if held to maturity |
| **Duration / Mod. Duration** | `DURATION`, `MDURATION` | `=DURATION(settle, mat, coupon, yld, 2, 0)` | Interest-rate sensitivity |

---

## 📈 Chapter 7 – Stock Valuation
| Concept / Formula               | Excel Function(s) | Example Syntax | What it Means                   |
| ------------------------------- | ----------------- | -------------- | ------------------------------- |
| **Zero-Growth DDM P₀ = D/r**    | —                 | `=D/r`         | Constant dividend forever       |
| **Gordon Growth P₀ = D₁/(r-g)** | —                 | `=D1/(r-g)`    | Dividend grows at constant rate |
| **Total Return ≈ D₁/P₀ + g**    | —                 | `=D1/P0 + g`   | Dividend yield + growth rate    |

---

## 🧮 Chapter 8 – NPV and Other Criteria
| Concept / Formula | Excel Function(s) | Example Syntax | What it Means |
|--------------------|------------------|----------------|---------------|
| **Profitability Index = PV inflows / C₀** | `NPV` | `=(NPV(r, inflows)+C0)/C0` | Value created per $ invested |
| **AAR = Avg NI / Avg BV** | `AVERAGE` | `=AVERAGE(NI)/AVERAGE(BV)` | Accounting rate of return |

---

## 🏗️ Chapter 9 – Making Capital Investment Decisions
| Concept / Formula | Excel Function(s) | Example Syntax | What it Means |
|--------------------|------------------|----------------|---------------|
| **OCF = (Sales–Costs)(1–Tc)+Dep×Tc** | — | `=(Sales-Costs)*(1-Tc)+Dep*Tc` | Operating cash flow |
| **After-Tax Salvage = SV-Tc(SV-BV)** | — | `=SV-Tc*(SV-BV)` | Sale proceeds net of tax |
| **CFFA = OCF – NCS – ΔNWC** | — | `=OCF-NCS-DeltaNWC` | Project free cash flow |
| **Depreciation methods** |
