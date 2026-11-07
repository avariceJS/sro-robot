// Synthetic test of the actual upstream/patched function extracted by the builder.
// No documents/models. Unsigned scalar sums explicitly model SIMD modulo 2^32.
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <random>
#include <vector>
extern "C" void kernel_old(const int8_t*, const float*, const int8_t*, int, float*, int);
extern "C" void kernel_dot(const int8_t*, const float*, const int8_t*, int, float*, int);
int main() {
  std::mt19937 rng(3471);
  std::uniform_int_distribution<int> random(-128,127);
  size_t comparisons=0;
  for (int raw : {0,1,7,8,9,31,64,95,128,159,160,232,384,511,1024,131071,131072,131073,262144})
  for (int out=1;out<=8;out++)
  for (int trial=0;trial<(raw>1024?4:100);trial++) {
    int n=(raw+7)/8*8;
    // Unaligned weight/input origins plus readable boundary canaries.
    std::vector<int8_t> weights(n*8+10, 23), input(n+2, 41);
    int8_t *w=weights.data()+1, *u=input.data()+1;
    std::array<float,8> scales;
    for(int j=0;j<n*8+8;j++) w[j]=trial==0?-128:trial==1?127:static_cast<int8_t>(random(rng));
    for(int j=0;j<n;j++) u[j]=j>=raw?0:trial==0?-128:trial==1?127:static_cast<int8_t>(random(rng));
    for(auto &scale:scales) scale=std::uniform_real_distribution<float>(-.1f,.1f)(rng);
    std::array<float,10> expected, actual;
    expected.fill(-999.f);
    for(int row=0;row<out;row++) {
      uint32_t sum=static_cast<uint32_t>(127*static_cast<int>(w[n*8+row]));
      for(int j=0;j<n;j++) sum+=static_cast<uint32_t>(static_cast<int>(w[(j/8)*64+row*8+j%8])*static_cast<int>(u[j]));
      int32_t signed_sum; std::memcpy(&signed_sum,&sum,4);
      expected[row+1]=static_cast<float>(signed_sum)*scales[row];
    }
    for(auto function:{kernel_old,kernel_dot}) {
      actual.fill(-999.f); function(w,scales.data(),u,n,actual.data()+1,out);
      if(std::memcmp(expected.data(),actual.data(),sizeof(actual)) || weights.front()!=23 || weights.back()!=23 || input.front()!=41 || input.back()!=41) {
        std::printf("FAIL width=%d tail=%d trial=%d\n",raw,out,trial);return 1;
      }
      comparisons++;
    }
  }
  std::printf("actual_source_scalar_comparisons=%zu exact=true\n",comparisons);
}
