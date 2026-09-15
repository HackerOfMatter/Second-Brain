📈 Profitability Index (PI)
Formula
𝑃
𝐼
=
PV of future cash inflows
Initial investment
PI=
Initial investment
PV of future cash inflows
	​


Decision Rule:

Accept if PI > 1.0

Reject if PI < 1.0

Meaning: NPV per dollar invested — useful when capital is limited.

Advantages

Accounts for time value of money

Useful for ranking projects with capital rationing

Disadvantages

Can give conflicting rankings compared to NPV when projects differ in scale.

🔁 MIRR (Modified Internal Rate of Return)
Purpose

Solves IRR’s reinvestment-rate problem by separating:

Reinvestment rate for compounding inflows

Financing rate for discounting outflows

Steps

Discount all outflows to time 0 using financing rate.

Compound all inflows to project end using reinvestment rate.

Compute the rate that equates them:

=MIRR(values, finance_rate, reinvest_rate)
𝑀
𝐼
𝑅
𝑅
=
(
𝐹
𝑉
inflows
𝑃
𝑉
outflows
)
1
/
𝑛
−
1
MIRR=(
PV
outflows
	​

FV
inflows
	​

	​

)
1/n
−1
Advantages

Reflects a more realistic reinvestment rate

Avoids multiple IRRs

Aligns better with NPV decisions

Disadvantages

Slightly more complex to compute

Requires assumption of reinvestment and financing rates

In Excel
=MIRR(values, finance_rate, reinvest_rate)