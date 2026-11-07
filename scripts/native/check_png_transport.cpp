// Compare the actual patched ImageData API with the independent old PNG round trip.
// Synthetic pixels only: no model, sample document or service is involved.
#include "imagedata.h"
#include <allheaders.h>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>
using tesseract::Image;
using tesseract::ImageData;
static std::vector<l_uint8> reference(PIX *pix) {
  l_uint8 *data=nullptr; size_t size=0;
  l_ok result=pixWriteMem(&data,&size,pix,IFF_PNG);
  if(result) result=pixWriteMem(&data,&size,pix,IFF_PNM);
  pixDestroy(&pix);
  if(result || !data || !size) std::abort();
  std::vector<l_uint8> bytes(data,data+size);lept_free(data);return bytes;
}
static std::string text(PIX *pix) { auto value=pixGetText(pix);return value?value:""; }
static bool equal(PIX *a, PIX *b) {
  l_int32 same=0;pixEqualWithAlpha(a,b,1,&same);
  return same && pixGetDepth(a)==pixGetDepth(b) && pixGetSpp(a)==pixGetSpp(b) &&
         pixGetXRes(a)==pixGetXRes(b) && pixGetYRes(a)==pixGetYRes(b) &&
         pixGetInputFormat(a)==pixGetInputFormat(b) && text(a)==text(b);
}
static bool check(PIX *base, bool original_encoding=false, bool optimized=false) {
  auto bytes=reference(pixClone(base));
  PIX *expected=pixReadMem(bytes.data(),bytes.size());
  ImageData candidate(false,Image(pixClone(base)));
  Image actual=candidate.GetPix();
  bool same=equal(expected,actual) && reference(pixClone(base))==bytes;
  if(original_encoding) same=same && candidate.MemoryUsed()==static_cast<int>(bytes.size());
  if(optimized) same=same && candidate.MemoryUsed()>static_cast<int>(bytes.size());
  actual.destroy();pixDestroy(&expected);return same;
}
static int failures_left=0, allocation_failures=0, raster_calls=0;
static void *fail_once(size_t size) {
  raster_calls++;
  if(failures_left) {failures_left--;allocation_failures++;return nullptr;}
  return std::malloc(size);
}
int main() {
  int comparisons=0;
  for(int depth:{1,2,4,8,16,32}) for(int palette=0;palette<2;palette++)
  for(int alpha=0;alpha<2;alpha++) for(int dpi:{0,96,300}) for(int textual=0;textual<2;textual++) {
    if((palette && depth>8) || (alpha && depth!=32 && !palette)) continue;
    PIX *base=pixCreate(37,19,depth);
    if(depth==32) pixSetSpp(base,alpha?4:3);
    if(palette) {
      PIXCMAP *cmap=pixcmapCreate(depth);
      for(int i=0;i<(1<<depth);i++) pixcmapAddRGBA(cmap,(i*79)%256,(i*31)%256,(i*117)%256,alpha?(i*47)%256:255);
      pixSetColormap(base,cmap);
    }
    for(int y=0;y<19;y++) for(int x=0;x<37;x++) {
      l_uint32 value=depth==32?(x*1234567U+y*99887123U):((x*13+y*7)&((1U<<depth)-1));
      pixSetPixel(base,x,y,value);
    }
    pixSetResolution(base,dpi,dpi?dpi+7:0);
    if(textual) pixSetText(base,"synthetic metadata");
    pixSetZlibCompression(base,6);
    if(!check(base,palette || textual)) {std::printf("FAIL depth=%d palette=%d alpha=%d dpi=%d text=%d\n",depth,palette,alpha,dpi,textual);return 1;}
    pixDestroy(&base);comparisons++;
  }
  // Beyond the 8-MiB raw-pixel budget the original PNG encoding is retained.
  PIX *large=pixCreate(4096,1024,32);pixSetAll(large);
  if(!check(large,true)) {std::puts("FAIL bounded-original-encoding");return 1;}
  pixDestroy(&large);comparisons++;
  // Metadata-free input must actually use PNG0, while a shared alias remains intact.
  PIX *eligible=pixCreate(256,128,8);pixSetAll(eligible);pixSetZlibCompression(eligible,6);
  if(!check(eligible,false,true)) {std::puts("FAIL eligible-branch-not-used");return 1;}
  pixDestroy(&eligible);comparisons++;
  // Metadata must avoid the new copy entirely, including under allocation faults.
  // Match the old encoder's allocation schedule/fallback result; an added partial
  // Pix copy would add a raster allocation and could lose its palette or text.
  PIX *metadata=pixCreate(128,32,8);PIXCMAP *cmap=pixcmapCreate(8);
  pixcmapAddColor(cmap,10,20,30);pixSetColormap(metadata,cmap);pixSetText(metadata,"synthetic");
  failures_left=1;allocation_failures=0;raster_calls=0;setPixMemoryManager(fail_once,std::free);
  auto original_fault=reference(pixClone(metadata));
  setPixMemoryManager(std::malloc,std::free);
  int original_calls=raster_calls,original_failures=allocation_failures;
  failures_left=1;allocation_failures=0;raster_calls=0;setPixMemoryManager(fail_once,std::free);
  ImageData metadata_fault(false,Image(pixClone(metadata)));
  setPixMemoryManager(std::malloc,std::free);
  PIX *metadata_expected=pixReadMem(original_fault.data(),original_fault.size());
  Image metadata_actual=metadata_fault.GetPix();
  bool metadata_valid=original_calls==raster_calls && original_failures==allocation_failures &&
      metadata_fault.MemoryUsed()==static_cast<int>(original_fault.size()) && equal(metadata_expected,metadata_actual);
  metadata_actual.destroy();pixDestroy(&metadata_expected);pixDestroy(&metadata);
  if(!metadata_valid) {std::puts("FAIL metadata-copy-failure-avoidance");return 1;}
  comparisons++;
  // Fail the actual new pixel-copy allocation; original PNG must still work.
  PIX *base=pixCreate(37,19,8);pixSetAll(base);
  auto bytes=reference(pixClone(base));PIX *expected=pixReadMem(bytes.data(),bytes.size());
  failures_left=1;allocation_failures=0;setPixMemoryManager(fail_once,std::free);
  ImageData fallback(false,Image(pixClone(base)));
  setPixMemoryManager(std::malloc,std::free);
  Image actual=fallback.GetPix();
  bool valid=allocation_failures==1 && equal(expected,actual) && fallback.MemoryUsed()==static_cast<int>(bytes.size());
  actual.destroy();pixDestroy(&expected);pixDestroy(&base);
  if(!valid) {std::puts("FAIL copy-allocation-fallback");return 1;}
  comparisons++;
  std::printf("actual_ImageData_reference_metadata_alias_budget_failure_checks=%d exact=true\n",comparisons);
}
