# 📑 Corporate Finance Formula Finder (Question → Formula → Excel)

Use this to instantly find which formula or Excel function applies based on **what the question gives you** or **asks for**.

---
[[Finance Abriviations]]



## 🔹 STOCK VALUATION (Ch. 7)

### 🟢 "Given D₁, P₀, and g → Find r (Required Return)"
**Formula:**  
r = (D₁ / P₀) + g  
**Excel:**  
`=(D1 / P0) + g`  
**Meaning:** Required return for a stock with constant dividend growth.  
**Example:** D₁ = 2.85, P₀ = 49.30, g = 5% → r = (2.85 / 49.30) + 0.05 = **0.1078 → 10.78%**

---

### 🟢 "Given D₁, r, and g → Find intrinsic value (P₀)"
**Formula:**  
P₀ = D₁ / (r – g)  
**Excel:**  
`=D1 / (r - g)`  
**Meaning:** Gordon Growth (Constant Growth DDM).  
**Used for:** Dividend-paying, mature companies.

---

### 🟢 "Given D, r → Find price for zero-growth stock (preferred)"
**Formula:**  
P₀ = D / r  
**Excel:**  
`=D / r`  
**Meaning:** Zero-growth DDM or perpetuity — used for preferred stock.  

---

### 🟢 "Given P₀, D₁ → Find Dividend Yield"
**Formula:**  
Dividend Yield = D₁ / P₀  
**Excel:**  
`=D1 / P0`  
**Meaning:** Portion of return from dividends.

---

### 🟢 "Given r, Dividend Yield → Find Growth"
**Formula:**  
g = r – (D₁ / P₀)  
**Excel:**  
`=r - (D1 / P0)`  
**Meaning:** Rearranged Gordon model — useful when total return and dividend yield known.  

---

## 🔹 TIME VALUE OF MONEY (Ch. 4)

### 🟢 "Given PV, r, t → Find FV"
**Formula:**  
FV = PV(1 + r)^t  
**Excel:**  
`=FV(r, t, 0, -PV)`  
**Meaning:** Value t periods into the future.

---

### 🟢 "Given FV, r, t → Find PV"
**Formula:**  
PV = FV / (1 + r)^t  
**Excel:**  
`=PV(r, t, 0, -FV)`  
**Meaning:** Present value of a future amount.

---

### 🟢 "Given C, r → Find PV of perpetuity"
**Formula:**  
PV = C / r  
**Excel:**  
`=C / r`  
**Meaning:** Present value of level cash flow forever (e.g., preferred stock).  

---

### 🟢 "Given C₁, r, g → Find PV of growing perpetuity"
**Formula:**  
PV = C₁ / (r – g)  
**Excel:**  
`=C1 / (r - g)`  
**Meaning:** Perpetual cash flow growing at g (r>g).  

---

### 🟢 "Given r, t, C → Find PV of an annuity"
**Formula:**  
PV = C[1 – (1 + r)^–t] / r  
**Excel:**  
`=PV(r, t, -C, 0)`  
**Meaning:** Value of a level payment series.

---

## 🔹 NPV & PROJECT EVALUATION (Ch. 8–9)

### 🟢 "Given cash flows and discount rate → Find NPV"
**Formula:**  
NPV = Σ CFₜ / (1 + r)^t – C₀  
**Excel:**  
`=NPV(r, CF1:CFn) + C0`  
**Meaning:** Present value of inflows minus cost.

---

### 🟢 "Find IRR for a series of cash flows"
**Formula:**  
0 = Σ CFₜ / (1 + r)^t – C₀  
**Excel:**  
`=IRR(range_of_CFs)`  
**Meaning:** Discount rate where NPV = 0.

---

### 🟢 "Find Profitability Index"
**Formula:**  
PI = (NPV inflows + C₀) / C₀  
**Excel:**  
`=(NPV(r, inflows) + C0) / C0`  
**Meaning:** Value created per $ invested.

---

### 🟢 "Compute Operating Cash Flow (Tax Shield method)"
**Formula:**  
OCF = (Sales – Costs)(1 – Tc) + Dep×Tc  
**Excel:**  
`=(Sales - Costs)*(1 - Tc) + Dep*Tc`  
**Meaning:** Project’s after-tax cash flow from operations.  

---

### 🟢 "Find Project Free Cash Flow (CFFA)"
**Formula:**  
CFFA = OCF – NCS – ΔNWC  
**Excel:**  
`=OCF - NCS - DeltaNWC`  
**Meaning:** Cash available to investors after investments.

---

## 🔹 BOND VALUATION (Ch. 6)

### 🟢 "Given bond details → Find Price"
**Formula:**  
P = Σ C/(1+r)^t + F/(1+r)^T  
**Excel:**  
`=PRICE(settle, maturity, coupon, yld, 100, freq, basis)`  
**Meaning:** PV of all future coupon and principal payments.  

---

### 🟢 "Given bond price → Find YTM"
**Formula:**  
YTM solves NPV=0 for bond cash flows.  
**Excel:**  
`=YIELD(settle, maturity, coupon, price, 100, freq, basis)`  
**Meaning:** Annual return if bond held to maturity.  

---

### 🟢 "Find Current Yield"
**Formula:**  
CY = Coupon / Price  
**Excel:**  
`=Coupon / Price`  
**Meaning:** Measures annual income relative to price.

---

## 🔹 RISK & RETURN (Ch. 10–11)

### 🟢 "Find Expected Return of a stock"
**Formula:**  
E[R] = Σ pᵢRᵢ  
**Excel:**  
`=SUMPRODUCT(probabilities, returns)`  
**Meaning:** Weighted average of possible returns.

---

### 🟢 "Find Portfolio Expected Return"
**Formula:**  
E[Rₚ] = Σ wᵢE[Rᵢ]  
**Excel:**  
`=SUMPRODUCT(weights, returns)`  
**Meaning:** Weighted average across holdings.  

---

### 🟢 "Find Standard Deviation or Variance"
**Formula:**  
σ = √Var = √(Σ pᵢ(Rᵢ – E[R])²)  
**Excel:**  
`=STDEV.S(range)` | variance `=VAR.S(range)`  
**Meaning:** Measures return volatility (risk).

---

### 🟢 "Find Beta or Required Return (CAPM)"
**Formula:**  
r = Rf + β(E[Rm] – Rf)  
**Excel:**  
`=Rf + Beta*(Rm - Rf)`  
**Meaning:** Required return for given risk exposure.  

**Find β:**  
β = Cov(Ri, Rm) / Var(Rm) → `=SLOPE(asset_returns, market_returns)`  

---

## 🔹 COST OF CAPITAL (Ch. 12)

### 🟢 "Find WACC"
**Formula:**  
WACC = (E/V)Re + (D/V)Rd(1–Tc)  
**Excel:**  
`=(E/V)*Re + (D/V)*Rd*(1-Tc)`  
**Meaning:** Firm’s blended cost of financing.

---

### 🟢 "Find Cost of Equity (Dividend Model)"
**Formula:**  
Re = D₁/P₀ + g  
**Excel:**  
`=D1 / P0 + g`  
**Meaning:** Investor’s required equity return via DDM.

---

### 🟢 "Find Cost of Equity (CAPM)"
**Formula:**  
Re = Rf + β(MRP)  
**Excel:**  
`=Rf + Beta*MRP`  
**Meaning:** Required return based on risk and market premium.

---

### 🟢 "Find After-Tax Cost of Debt"
**Formula:**  
Rd(1–Tc)  
**Excel:**  
`=YIELD(...)*(1 - Tc)`  
**Meaning:** Debt cost net of tax shield.

---

## 🔹 GROWTH & DIVIDENDS (Ch. 14)

### 🟢 "Find Payout Ratio"
**Formula:**  
Payout = Div / NI  
**Excel:**  
`=Div / NetIncome`  

### 🟢 "Find Retention Ratio"
**Formula:**  
b = 1 – Payout  
**Excel:**  
`=1 - Payout`  

### 🟢 "Find Sustainable Growth Rate"
**Formula:**  
SGR = ROE × b  
**Excel:**  
`=ROE * Retention`  
**Meaning:** Max growth without new external equity.

---

## 🔹 SHORT-TERM FINANCE & CASH CYCLE (Ch. 16)

### 🟢 "Find Operating Cycle"
**Formula:**  
Operating = Inventory Days + Receivable Days  
**Excel:**  
`=InvDays + RecDays`  

### 🟢 "Find Cash Cycle"
**Formula:**  
Cash = Operating – Payables Days  
**Excel:**  
`=Operating - PayableDays`  

---

## ⚙️ EXCEL PITFALLS & NOTES
- `NPV` assumes equal periods; use `XNPV` for dated cash flows.  
- Always use **negative sign for outflows**.  
- Match **r** to period timing (annual vs monthly).  
- For bonds, specify **settlement, maturity, coupon, yield, redemption, frequency, basis**.  
- Use `SLOPE(asset, market)` for beta from excess returns.  
- Depreciation: `=SLN()`, `=SYD()`, `=DB()`. For MACRS, use IRS table rates.

---

💡 **Pro Tip:** Search (`Cmd/Ctrl+F`) for keywords like *“required return”*, *“NPV”*, *“perpetuity”*, or *“cash flow”* — each section header above matches problem language directly.
